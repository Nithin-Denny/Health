"""Shared SQLite access, schema initialization, audit helpers and synthetic data.

No cached/global connection. Every public operation opens a short-lived connection.
Only scheduler/integration services should mutate bookings in team dashboard code.
"""
from contextlib import contextmanager
import argparse
import json
import os
from pathlib import Path
import sqlite3

from errors import NotFoundError, SchemaMismatchError, ValidationError
from time_utils import clinic_datetime, next_demo_day, to_storage, utc_now

ROOT = Path(__file__).resolve().parent
SCHEMA_VERSION = "1"
OPEN_PROPOSAL_STATES = ("pending", "doctor_approved", "patient_agreed", "ready")
TABLE_IDS = {
    "appointments": "appointment_id", "patients": "patient_id", "doctors": "doctor_id",
    "scheduling_proposals": "proposal_id", "appointment_requests": "request_id",
    "staff_review_tasks": "task_id",
}


def get_db_path(db_path=None):
    raw = db_path if db_path is not None else os.environ.get("CLINIC_DB_PATH", ROOT / "clinic.db")
    if str(raw) == ":memory:":
        raise ValidationError("Use a temporary database file, not :memory:, with per-operation connections.")
    return Path(raw).expanduser().resolve()


def connect(db_path=None):
    path = get_db_path(db_path)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def read_connection(db_path=None):
    conn = connect(db_path)
    try:
        conn.execute("BEGIN")  # One consistent snapshot for multi-query dashboards.
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def transaction(db_path=None):
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")  # Serialize read-check-write booking changes.
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize_database(db_path=None):
    """Create v1, or validate v1. Refuse incompatible team schemas without changing data."""
    path = get_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    # Derive expected columns from the actual SQL, not a second conflicting definition.
    expected = sqlite3.connect(":memory:")
    try:
        expected.executescript(schema)
        tables = [r[0] for r in expected.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        columns = {name: {r[1] for r in expected.execute(f'PRAGMA table_info("{name}")')}
                   for name in tables}  # Names come only from our fixed schema.
    finally:
        expected.close()
    conn = connect(path)
    try:
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        recognized = existing.intersection(columns)
        for name in recognized:
            actual = {r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')}
            missing = columns[name] - actual
            if missing:
                raise SchemaMismatchError(
                    f"Existing {name} is incompatible: missing {', '.join(sorted(missing))}. "
                    "No tables were replaced. Back up and agree a team migration; see CONTRACT.md.")
        if recognized:
            if "schema_meta" not in existing:
                raise SchemaMismatchError("Existing team tables have no v1 schema marker. "
                                          "Back up and explicitly migrate them; nothing was replaced.")
            row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            if row is None or row[0] != SCHEMA_VERSION:
                raise SchemaMismatchError("Unsupported schema version. No automatic destructive migration is performed.")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript("BEGIN IMMEDIATE;\n" + schema +
                           "\nINSERT OR IGNORE INTO schema_meta(key,value) VALUES('schema_version','1');\nCOMMIT;")
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return str(path)


def row_to_dict(row):
    if row is None:
        return None
    result = dict(row)
    for key, target in (("risk_flags", "risk_flags"), ("questionnaire_json", "questionnaire"),
                        ("details_json", "details")):
        if key in result:
            try:
                decoded = json.loads(result[key] or ("[]" if key == "risk_flags" else "{}"))
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValidationError(f"Invalid stored JSON in {key}; staff must review this record.") from exc
            if key == "risk_flags" and not isinstance(decoded, list):
                raise ValidationError("Stored risk_flags must be a JSON list.")
            if key != "risk_flags" and not isinstance(decoded, dict):
                raise ValidationError(f"Stored {key} must be a JSON object.")
            if target != key:
                del result[key]
            result[target] = decoded
    return result


def required_row(conn, table, record_id):
    if table not in TABLE_IDS:
        raise ValueError("Unknown internal table")
    row = conn.execute(f"SELECT * FROM {table} WHERE {TABLE_IDS[table]} = ?", (record_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"{table.replace('_', ' ').title()} record {record_id!r} was not found.")
    return row_to_dict(row)


def get_appointment(appointment_id, db_path=None):
    with read_connection(db_path) as conn:
        return required_row(conn, "appointments", appointment_id)


def get_patient(patient_id, db_path=None):
    with read_connection(db_path) as conn:
        return required_row(conn, "patients", patient_id)


def get_doctor(doctor_id, db_path=None):
    with read_connection(db_path) as conn:
        return required_row(conn, "doctors", doctor_id)


def list_doctors(db_path=None):
    with read_connection(db_path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM doctors WHERE active=1 ORDER BY doctor_id")]


def list_patients(db_path=None):
    with read_connection(db_path) as conn:
        return [row_to_dict(r) for r in conn.execute("SELECT * FROM patients ORDER BY patient_id")]


def list_appointments(patient_id=None, doctor_id=None, db_path=None):
    conditions, values = [], []
    if patient_id is not None:
        conditions.append("a.patient_id=?")
        values.append(patient_id)
    if doctor_id is not None:
        conditions.append("a.doctor_id=?")
        values.append(doctor_id)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with read_connection(db_path) as conn:
        return [row_to_dict(r) for r in conn.execute(
            "SELECT a.*, p.display_name AS patient_name, d.display_name AS doctor_name "
            "FROM appointments a JOIN patients p ON p.patient_id=a.patient_id "
            "JOIN doctors d ON d.doctor_id=a.doctor_id" + where +
            " ORDER BY a.priority_score DESC, a.appointment_datetime, a.appointment_id", values)]


def log_action(conn, appointment_id, action_type, reason, actor="system", *,
               proposal_id=None, details=None, now=None):
    conn.execute(
        "INSERT INTO action_logs(appointment_id,proposal_id,action_type,reason,actor,details_json,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (appointment_id, proposal_id, action_type, str(reason), actor,
         json.dumps(details or {}, ensure_ascii=False), to_storage(utc_now(now))))


def log_failure(action_type, reason, *, appointment_id=None, actor="system", details=None,
                db_path=None, now=None):
    """Best effort AFTER rollback. Database outages cannot themselves be audited in that DB."""
    try:
        with transaction(db_path) as conn:
            if appointment_id is not None and conn.execute(
                    "SELECT 1 FROM appointments WHERE appointment_id=?", (appointment_id,)).fetchone() is None:
                appointment_id = None
            log_action(conn, appointment_id, action_type, reason, actor, details=details, now=now)
        return True
    except (sqlite3.Error, OSError):
        return False  # Never mask the original operation failure.


def list_action_logs(appointment_id=None, limit=100, db_path=None):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 2000:
        raise ValidationError("Log limit must be from 1 to 2000.")
    with read_connection(db_path) as conn:
        if appointment_id is None:
            rows = conn.execute("SELECT * FROM action_logs ORDER BY log_id DESC LIMIT ?", (limit,))
        else:
            rows = conn.execute("SELECT * FROM action_logs WHERE appointment_id=? ORDER BY log_id DESC LIMIT ?",
                                (appointment_id, limit))
        return [row_to_dict(r) for r in rows]


def ensure_staff_task(conn, appointment_id, task_type, reason, *, now=None):
    old = conn.execute("SELECT * FROM staff_review_tasks WHERE appointment_id=? AND task_type=? AND task_status='open'",
                       (appointment_id, task_type)).fetchone()
    if old:
        return dict(old)
    stamp = to_storage(utc_now(now))
    cursor = conn.execute(
        "INSERT INTO staff_review_tasks(appointment_id,task_type,reason,created_at,updated_at) VALUES(?,?,?,?,?)",
        (appointment_id, task_type, reason, stamp, stamp))
    log_action(conn, appointment_id, "staff_review_created", reason, details={"task_type": task_type}, now=now)
    return required_row(conn, "staff_review_tasks", cursor.lastrowid)


def resolve_tasks(conn, appointment_id, task_types, resolution, *, now=None):
    for task_type in task_types:
        rows = conn.execute("SELECT task_id FROM staff_review_tasks WHERE appointment_id=? AND task_type=? AND task_status='open'",
                            (appointment_id, task_type)).fetchall()
        for row in rows:
            conn.execute("UPDATE staff_review_tasks SET task_status='resolved',resolution=?,updated_at=? WHERE task_id=?",
                         (resolution, to_storage(utc_now(now)), row[0]))
            log_action(conn, appointment_id, "staff_review_resolved", resolution,
                       details={"task_id": row[0]}, now=now)


def seed_reference_data(db_path=None, now=None):
    """Only synthetic identities and fixed Mon-Sat 09-12 / 13-17 availability."""
    stamp = to_storage(utc_now(now))
    with transaction(db_path) as conn:
        for doctor_id, name in (("D001", "Dr Demo One"), ("D002", "Dr Demo Two")):
            conn.execute("INSERT OR IGNORE INTO doctors(doctor_id,display_name) VALUES(?,?)", (doctor_id, name))
            for weekday in range(6):
                for begin, end in (("09:00", "12:00"), ("13:00", "17:00")):
                    conn.execute("INSERT OR IGNORE INTO doctor_availability "
                                 "(doctor_id,weekday,start_time,end_time,slot_interval_minutes) VALUES(?,?,?,?,30)",
                                 (doctor_id, weekday, begin, end))
        patients = (("P001", "Demo Routine A", "routine"), ("P002", "Demo Routine B", "routine"),
                    ("P003", "Demo Priority Intake", "priority"), ("P004", "Demo Urgent Visit", "urgent"),
                    ("P005", "Demo Routine C", "routine"), ("P006", "Demo Engine Failure", "engine_error"))
        for patient_id, name, scenario in patients:
            conn.execute("INSERT OR IGNORE INTO patients "
                         "(patient_id,display_name,questionnaire_json,mock_scenario,created_at) VALUES(?,?,?,?,?)",
                         (patient_id, name, json.dumps({"synthetic": True, "fixture": scenario}), scenario, stamp))


def seed_demo_data(db_path=None, now=None, *, reminder_sync=None):
    """Idempotent fixtures. Caller may inject a transactional reminder callback.

    This database library never imports higher-level services. CLI/app composition
    supplies the callback so synthetic bookings and reminders are seeded together.
    Existing approvals, appointments, histories and consent are never reset.
    """
    initialize_database(db_path)
    seed_reference_data(db_path, now)
    with transaction(db_path) as conn:
        meta = conn.execute("SELECT value FROM schema_meta WHERE key='demo_day'").fetchone()
        day = meta[0] if meta else next_demo_day(now).isoformat()
        if not meta:
            conn.execute("INSERT INTO schema_meta(key,value) VALUES('demo_day',?)", (day,))
        stamp = to_storage(utc_now(now))
        fixtures = (
            ("routine_a", "P001", "D001", "09:00", "booked", 20, "routine", 0),
            ("routine_b", "P002", "D001", "09:30", "booked", 25, "routine", 0),
            ("urgent_visit", "P004", "D001", "10:00", "booked", 95, "urgent", 1),
            ("priority_intake", "P003", "D001", "09:00", "requested", None, "unknown", 0),
            ("other_doctor", "P005", "D002", "10:00", "booked", 20, "routine", 0),
        )
        ids = {}
        for key, patient_id, doctor_id, clock, status, score, urgency, protected in fixtures:
            existing = conn.execute("SELECT appointment_id FROM appointments WHERE demo_key=?", (key,)).fetchone()
            if existing:
                ids[key] = existing[0]
                if reminder_sync is not None:
                    reminder_sync(conn, required_row(conn, "appointments", existing[0]), now=now)
                continue
            start = to_storage(clinic_datetime(day, clock))
            ready = status == "booked"
            flags = ["requires_staff_review"] if urgency == "urgent" else []
            cursor = conn.execute(
                "INSERT INTO appointments(patient_id,doctor_id,appointment_datetime,requested_datetime,"
                "latest_acceptable_datetime,duration_minutes,appointment_status,confirmed,priority_score,risk_flags,"
                "agent_reason,recommended_action,urgency,evaluation_status,priority_evaluated_at,engine_source,"
                "protected,demo_key,created_at,updated_at) VALUES(?,?,?,?,?,30,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (patient_id, doctor_id, start, start, start, status, int(ready), score, json.dumps(flags),
                 "Synthetic fixed fixture; not a clinical assessment." if ready else None,
                 "escalate_to_staff" if urgency == "urgent" else ("routine_booking" if ready else None),
                 urgency, "ready" if ready else "pending", stamp if ready else None,
                 "mock-fixture-v1" if ready else None, protected, key, stamp, stamp))
            ids[key] = cursor.lastrowid
            log_action(conn, cursor.lastrowid, "demo_appointment_seeded", "Synthetic demo record created.", "demo-seed", now=now)
            if ready and reminder_sync is not None:
                reminder_sync(conn, required_row(conn, "appointments", cursor.lastrowid), now=now)
        return {"date": day, "doctor_id": "D001", "incoming_appointment_id": ids["priority_intake"],
                "affected_appointment_id": ids["routine_a"], "appointment_ids": ids}


def checkpoint_database(db_path=None):
    """Make the main .db self-contained before copying/zipping (close app first on Windows)."""
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialize the shared clinic database.")
    parser.add_argument("--db", help="Absolute path to the shared database")
    parser.add_argument("--seed", action="store_true", help="Add idempotent synthetic demo data")
    args = parser.parse_args()
    print("Database:", initialize_database(args.db))
    if args.seed:
        # Executable composition only: a normal `import database` has no upward imports.
        from reminder_manager import sync_reminders_in_transaction
        print("Demo:", json.dumps(seed_demo_data(args.db, reminder_sync=sync_reminders_in_transaction), indent=2))
    checkpoint_database(args.db)
