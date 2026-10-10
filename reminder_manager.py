"""Transactional, simulated 48h/24h reminders. No email/SMS provider or worker.

Dashboard refresh invokes process_due_reminders(). It is also safe to run from two
local Streamlit processes: an immediate write transaction claims and finishes each
simulated reminder atomically. External delivery would require an outbox/idempotent
provider; this prototype intentionally has no such external side effect.
"""
from datetime import timedelta
import sqlite3

import database as db
from errors import ValidationError
from time_utils import as_utc, display_datetime, to_storage, utc_now

REMINDER_OFFSETS = (("48h", 48), ("24h", 24))


def _message(appointment, reminder_type):
    return (f"SIMULATION ONLY — {reminder_type} reminder: appointment "
            f"#{appointment['appointment_id']} with {appointment['doctor_id']} at "
            f"{display_datetime(appointment['appointment_datetime'])} Asia/Kolkata. "
            "No SMS or email was sent.")


def skip_pending_in_transaction(conn, appointment_id, reason, *, now=None):
    stamp = to_storage(utc_now(now))
    rows = conn.execute("SELECT reminder_id FROM reminders WHERE appointment_id=? AND status='scheduled'",
                        (appointment_id,)).fetchall()
    for row in rows:
        conn.execute("UPDATE reminders SET status='skipped',processed_at=?,status_reason=? WHERE reminder_id=?",
                     (stamp, reason, row[0]))
        db.log_action(conn, appointment_id, "reminder_skipped", reason,
                      details={"reminder_id": row[0]}, now=now)


def sync_reminders_in_transaction(conn, appointment, *, now=None):
    """Called inside booking/rescheduling transaction so appointment + reminders agree."""
    clock = utc_now(now)
    stamp = to_storage(clock)
    appointment_id = appointment["appointment_id"]
    if appointment["appointment_status"] != "booked" or not appointment["confirmed"]:
        skip_pending_in_transaction(conn, appointment_id, "Appointment is not an active confirmed booking.", now=clock)
        return
    start = as_utc(appointment["appointment_datetime"])
    # Preserve sent history, invalidate only outstanding reminders for old starts.
    old = conn.execute("SELECT reminder_id FROM reminders WHERE appointment_id=? "
                       "AND appointment_datetime!=? AND status='scheduled'",
                       (appointment_id, appointment["appointment_datetime"])).fetchall()
    for row in old:
        conn.execute("UPDATE reminders SET status='skipped',processed_at=?,status_reason=? WHERE reminder_id=?",
                     (stamp, "Appointment rescheduled; this reminder was superseded.", row[0]))
        db.log_action(conn, appointment_id, "reminder_skipped", "Old schedule superseded by a confirmed rescheduling.",
                      details={"reminder_id": row[0]}, now=clock)
    for reminder_type, hours in REMINDER_OFFSETS:
        scheduled = start - timedelta(hours=hours)
        # A new appointment less than N hours away does not cause an immediate,
        # misleading N-hour notification. Retain an explicit missed-window record.
        missed = scheduled < clock
        status = "skipped" if missed else "scheduled"
        reason = "Created after this reminder's window; no retroactive delivery." if missed else None
        cursor = conn.execute(
            "INSERT OR IGNORE INTO reminders(appointment_id,appointment_datetime,reminder_type,scheduled_at,"
            "status,message,processed_at,status_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (appointment_id, appointment["appointment_datetime"], reminder_type, to_storage(scheduled),
             status, _message(appointment, reminder_type), stamp if missed else None, reason, stamp))
        if cursor.rowcount:
            db.log_action(conn, appointment_id, "reminder_skipped" if missed else "reminder_scheduled",
                          reason or f"{reminder_type} simulated reminder scheduled.",
                          details={"reminder_type": reminder_type, "scheduled_at": to_storage(scheduled)}, now=clock)


def schedule_reminders(appointment_id, *, db_path=None, now=None):
    try:
        with db.transaction(db_path) as conn:
            appointment = db.required_row(conn, "appointments", appointment_id)
            sync_reminders_in_transaction(conn, appointment, now=now)
        return get_reminder_history(appointment_id=appointment_id, db_path=db_path)
    except Exception as exc:
        db.log_failure("operation_failed", f"Reminder scheduling failed: {exc}", appointment_id=appointment_id,
                       db_path=db_path, now=now)
        raise


def get_due_reminders(*, db_path=None, now=None, limit=500):
    _validate_limit(limit)
    with db.read_connection(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM reminders WHERE status='scheduled' AND scheduled_at<=? ORDER BY scheduled_at,reminder_id LIMIT ?",
            (to_storage(utc_now(now)), limit))]


def _validate_limit(limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 2000:
        raise ValidationError("Reminder batch limit must be from 1 to 2000.")


def process_due_reminders(*, db_path=None, now=None, limit=500):
    """At most once in SQLite; returns records processed by THIS invocation only."""
    _validate_limit(limit)
    clock = utc_now(now)
    stamp = to_storage(clock)
    processed_ids = []
    try:
        with db.transaction(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE status='scheduled' AND scheduled_at<=? "
                "ORDER BY scheduled_at,reminder_id LIMIT ?", (stamp, limit)).fetchall()
            for reminder in rows:
                appointment = db.required_row(conn, "appointments", reminder["appointment_id"])
                reason = None
                if appointment["appointment_status"] != "booked" or not appointment["confirmed"]:
                    reason = "Cancelled, completed, or unconfirmed appointment; notification skipped."
                elif appointment["appointment_datetime"] != reminder["appointment_datetime"]:
                    reason = "Reminder refers to a superseded appointment time."
                elif as_utc(appointment["appointment_datetime"]) <= clock:
                    reason = "Appointment has already started or passed; stale reminder skipped."
                status = "skipped" if reason else "simulated_sent"
                try:
                    message = _message(appointment, reminder["reminder_type"])
                except (ValueError, KeyError, TypeError) as exc:
                    status, reason, message = "failed", f"Simulation formatting failed: {exc}", reminder["message"]
                cursor = conn.execute(
                    "UPDATE reminders SET status=?,message=?,processed_at=?,status_reason=? "
                    "WHERE reminder_id=? AND status='scheduled'",
                    (status, message, stamp, reason, reminder["reminder_id"]))
                if cursor.rowcount:
                    processed_ids.append(reminder["reminder_id"])
                    db.log_action(conn, appointment["appointment_id"], f"reminder_{status}",
                                  reason or "Simulated notification recorded. No SMS/email was sent.",
                                  details={"reminder_id": reminder["reminder_id"], "message": message,
                                           "scheduled_at": reminder["scheduled_at"]}, now=clock)
            return [dict(conn.execute("SELECT * FROM reminders WHERE reminder_id=?", (rid,)).fetchone())
                    for rid in processed_ids]
    except (sqlite3.Error, ValidationError) as exc:
        db.log_failure("operation_failed", f"Reminder batch rolled back: {exc}", db_path=db_path, now=clock)
        raise


def get_reminder_history(appointment_id=None, patient_id=None, doctor_id=None, *, db_path=None):
    clauses, values = [], []
    for column, value in (("r.appointment_id", appointment_id), ("a.patient_id", patient_id), ("a.doctor_id", doctor_id)):
        if value is not None:
            clauses.append(column + "=?")
            values.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with db.read_connection(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT r.* FROM reminders r JOIN appointments a ON a.appointment_id=r.appointment_id" +
            where + " ORDER BY r.scheduled_at DESC,r.reminder_id DESC", values)]
