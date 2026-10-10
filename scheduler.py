"""Slots, conservative optimization, two-party proposals, and appointment changes.

Scheduling uses Member 3's persisted result; it contains no symptom-scoring model.
All mutation checks and writes happen under BEGIN IMMEDIATE. Proposals are NOT
reservations. Consent is tied to an immutable time and appointment-version snapshot.
"""
from datetime import timedelta
import sqlite3

import database as db
from errors import (ApprovalRequiredError, AuthorizationError, ConflictError,
                    HealthcareError, SlotUnavailableError, ValidationError)
from reminder_manager import skip_pending_in_transaction, sync_reminders_in_transaction
from time_utils import (as_utc, clinic_datetime, display_datetime, overlaps, parse_date,
                        to_local, to_storage, utc_now, validate_duration, validate_future_start)

MAX_INTAKE_WINDOW_DAYS = 14
MAX_MOVE_DAYS = 7
OPEN_SQL = "('pending','doctor_approved','patient_agreed','ready')"


def _doctor(conn, doctor_id):
    if not isinstance(doctor_id, str) or not doctor_id.strip():
        raise ValidationError("A doctor ID is required.")
    doctor = db.required_row(conn, "doctors", doctor_id)
    if not doctor["active"]:
        raise ValidationError("This doctor is not accepting bookings.")
    return doctor


def _patient(conn, patient_id):
    if not isinstance(patient_id, str) or not patient_id.strip():
        raise ValidationError("A patient ID is required.")
    return db.required_row(conn, "patients", patient_id)


def _reason(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValidationError("A reason of 1–4000 characters is required.")
    return value.strip()


def _windows(conn, doctor_id, day):
    return conn.execute("SELECT * FROM doctor_availability WHERE doctor_id=? AND weekday=? ORDER BY start_time",
                        (doctor_id, day.weekday())).fetchall()


def _grid_starts(conn, doctor_id, day, duration):
    """Generate only predefined clinic starts whose entire duration fits a window."""
    seen = set()
    for window in _windows(conn, doctor_id, day):
        start = as_utc(clinic_datetime(day, window["start_time"]))
        end = as_utc(clinic_datetime(day, window["end_time"]))
        while start + timedelta(minutes=duration) <= end:
            stamp = to_storage(start)
            if stamp not in seen:
                seen.add(stamp)
                yield start
            start += timedelta(minutes=window["slot_interval_minutes"])


def _fits_availability(conn, doctor_id, start, duration):
    day = to_local(start).date()
    return any(start == candidate for candidate in _grid_starts(conn, doctor_id, day, duration))


def _occupants(conn, doctor_id, start, duration, exclude_appointment_id=None):
    end = start + timedelta(minutes=duration)
    sql = ("SELECT * FROM appointments WHERE doctor_id=? AND appointment_status='booked' "
           "AND appointment_datetime < ? AND strftime('%Y-%m-%dT%H:%M:%SZ',appointment_datetime, "
           "'+' || duration_minutes || ' minutes') > ?")
    params = [doctor_id, to_storage(end), to_storage(start)]
    if exclude_appointment_id is not None:
        sql += " AND appointment_id != ?"
        params.append(exclude_appointment_id)
    return [db.row_to_dict(r) for r in conn.execute(sql, params)]


def _slot_available(conn, doctor_id, start, duration, clock, exclude_appointment_id=None):
    return (start > clock and _fits_availability(conn, doctor_id, start, duration)
            and not _occupants(conn, doctor_id, start, duration, exclude_appointment_id))


def _slot_dict(doctor_id, start, duration):
    return {"doctor_id": doctor_id, "appointment_datetime": to_storage(start),
            "clinic_datetime": to_local(start).isoformat(), "duration_minutes": duration,
            "end_datetime": to_storage(start + timedelta(minutes=duration))}


def _available_slots(conn, doctor_id, day, duration, clock, exclude_appointment_id=None):
    return [_slot_dict(doctor_id, start, duration)
            for start in _grid_starts(conn, doctor_id, day, duration)
            if start > clock and not _occupants(conn, doctor_id, start, duration, exclude_appointment_id)]


def get_available_slots(doctor_id, date, duration_minutes=None, *, db_path=None, now=None):
    day, clock = parse_date(date), utc_now(now)
    if day < to_local(clock).date():
        raise ValidationError("Available slots cannot be requested for a past clinic date.")
    with db.read_connection(db_path) as conn:
        doctor = _doctor(conn, doctor_id)
        duration = validate_duration(duration_minutes, doctor["default_duration_minutes"])
        return _available_slots(conn, doctor_id, day, duration, clock)


def is_slot_available(doctor_id, appointment_datetime, duration_minutes=None, *, db_path=None, now=None):
    clock = utc_now(now)
    start = validate_future_start(appointment_datetime, clock)
    with db.read_connection(db_path) as conn:
        doctor = _doctor(conn, doctor_id)
        duration = validate_duration(duration_minutes, doctor["default_duration_minutes"])
        return _slot_available(conn, doctor_id, start, duration, clock)


def _alternatives(conn, doctor_id, requested, duration, clock, *, days=14, limit=5,
                  exclude_appointment_id=None, avoid_interval=None):
    result = []
    for offset in range(days + 1):
        day = to_local(requested).date() + timedelta(days=offset)
        for slot in _available_slots(conn, doctor_id, day, duration, clock, exclude_appointment_id):
            start = as_utc(slot["appointment_datetime"])
            if start < requested:
                continue
            if avoid_interval and overlaps(start, duration, avoid_interval[0], avoid_interval[1]):
                continue
            result.append(slot)
            if len(result) >= limit:
                return result
    return result


def find_alternative_slots(doctor_id, requested_datetime, duration_minutes=None, *,
                           db_path=None, now=None, days=14, limit=5):
    if (isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 30
            or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50):
        raise ValidationError("Search days must be 0–30 and alternative limit must be 1–50.")
    clock = utc_now(now)
    requested = validate_future_start(requested_datetime, clock)
    with db.read_connection(db_path) as conn:
        doctor = _doctor(conn, doctor_id)
        duration = validate_duration(duration_minutes, doctor["default_duration_minutes"])
        return _alternatives(conn, doctor_id, requested, duration, clock, days=days, limit=limit)


def _new_values(conn, patient_id, doctor_id, appointment_datetime, duration_minutes,
                latest_acceptable_datetime, clock):
    _patient(conn, patient_id)
    doctor = _doctor(conn, doctor_id)
    duration = validate_duration(duration_minutes, doctor["default_duration_minutes"])
    start = validate_future_start(appointment_datetime, clock)
    latest = validate_future_start(latest_acceptable_datetime, clock) if latest_acceptable_datetime else start
    if latest < start or latest - start > timedelta(days=MAX_INTAKE_WINDOW_DAYS):
        raise ValidationError("Latest acceptable start must be from the requested start through 14 days later.")
    if not _fits_availability(conn, doctor_id, start, duration):
        raise SlotUnavailableError("Requested time/duration is outside this doctor's predefined slot grid.",
                                   _alternatives(conn, doctor_id, start, duration, clock))
    return start, latest, duration


def _insert(conn, patient_id, doctor_id, start, latest, duration, status, clock):
    stamp = to_storage(clock)
    cursor = conn.execute(
        "INSERT INTO appointments(patient_id,doctor_id,appointment_datetime,requested_datetime,"
        "latest_acceptable_datetime,duration_minutes,appointment_status,confirmed,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (patient_id, doctor_id, to_storage(start), to_storage(start), to_storage(latest), duration,
         status, int(status == "booked"), stamp, stamp))
    appointment = db.required_row(conn, "appointments", cursor.lastrowid)
    db.log_action(conn, cursor.lastrowid, "appointment_booked" if status == "booked" else "appointment_requested",
                  "Confirmed slot booked." if status == "booked" else "Intake saved; requested slots do not occupy the schedule.",
                  f"patient:{patient_id}", now=clock)
    if status == "booked":
        sync_reminders_in_transaction(conn, appointment, now=clock)
    return appointment


def _mutation_failure(exc, operation, appointment_id=None, actor="system", *, db_path=None, now=None, details=None):
    action = "booking_conflict" if isinstance(exc, (SlotUnavailableError, sqlite3.IntegrityError)) else "operation_failed"
    db.log_failure(action, f"{operation}: {exc}", appointment_id=appointment_id, actor=actor,
                   db_path=db_path, now=now, details=details)


def book_appointment(patient_id, doctor_id, appointment_datetime, duration_minutes=None, *, db_path=None, now=None):
    """Strict atomic slot booking. Integration.book_appointment also runs Member 3."""
    clock = utc_now(now)
    try:
        with db.transaction(db_path) as conn:
            start, latest, duration = _new_values(conn, patient_id, doctor_id, appointment_datetime,
                                                 duration_minutes, None, clock)
            if not _slot_available(conn, doctor_id, start, duration, clock):
                raise SlotUnavailableError("That slot is occupied. No booking was created.",
                                           _alternatives(conn, doctor_id, start, duration, clock))
            return _insert(conn, patient_id, doctor_id, start, latest, duration, "booked", clock)
    except Exception as exc:
        _mutation_failure(exc, "Booking failed", actor=f"patient:{patient_id}", db_path=db_path, now=clock,
                          details={"doctor_id": doctor_id, "requested": str(appointment_datetime)})
        if isinstance(exc, sqlite3.IntegrityError):
            raise SlotUnavailableError("Concurrent booking conflict. Nothing was booked; refresh the slots.") from exc
        if isinstance(exc, sqlite3.OperationalError):
            raise ConflictError("The shared database is busy/unavailable. Nothing was booked; retry after refreshing.") from exc
        raise


def create_appointment_request(patient_id, doctor_id, appointment_datetime, duration_minutes=None,
                               latest_acceptable_datetime=None, *, db_path=None, now=None):
    clock = utc_now(now)
    try:
        with db.transaction(db_path) as conn:
            start, latest, duration = _new_values(conn, patient_id, doctor_id, appointment_datetime,
                                                 duration_minutes, latest_acceptable_datetime, clock)
            old = conn.execute("SELECT * FROM appointments WHERE patient_id=? AND doctor_id=? "
                               "AND requested_datetime=? AND latest_acceptable_datetime=? AND duration_minutes=? "
                               "AND (appointment_status='requested' OR (appointment_status='booked' "
                               "AND appointment_datetime BETWEEN ? AND ?)) ORDER BY appointment_id LIMIT 1",
                               (patient_id, doctor_id, to_storage(start), to_storage(latest), duration,
                                to_storage(start), to_storage(latest))).fetchone()
            if old:
                return db.row_to_dict(old)
            return _insert(conn, patient_id, doctor_id, start, latest, duration, "requested", clock)
    except Exception as exc:
        _mutation_failure(exc, "Appointment intake failed", actor=f"patient:{patient_id}", db_path=db_path, now=clock)
        raise


def _targets(conn, incoming, clock):
    first = as_utc(incoming["requested_datetime"])
    last = as_utc(incoming["latest_acceptable_datetime"])
    day = to_local(first).date()
    last_day = to_local(last).date()
    while day <= last_day:
        for start in _grid_starts(conn, incoming["doctor_id"], day, incoming["duration_minutes"]):
            if first <= start <= last and start > clock:
                yield start
        day += timedelta(days=1)


def _requires_staff(appointment):
    return (bool(appointment["risk_flags"]) or appointment["urgency"] in ("urgent", "emergency")
            or appointment["recommended_action"] in ("escalate_to_staff", "emergency_review"))


def _incoming_eligible(appointment):
    return (appointment["appointment_status"] == "requested" and appointment["evaluation_status"] == "ready"
            and appointment["urgency"] == "priority" and appointment["recommended_action"] == "expedite"
            and not appointment["risk_flags"] and appointment["priority_score"] is not None)


def _routine_eligible(conn, appointment, clock, ignore_proposal_id=None):
    if (appointment["appointment_status"] != "booked" or not appointment["confirmed"]
            or appointment["evaluation_status"] != "ready" or appointment["urgency"] != "routine"
            or appointment["recommended_action"] != "routine_booking" or appointment["risk_flags"]
            or appointment["protected"] or not appointment["movable"]
            or appointment["priority_score"] is None or as_utc(appointment["appointment_datetime"]) <= clock):
        return False
    if conn.execute("SELECT 1 FROM appointment_requests WHERE appointment_id=? AND request_status='pending'",
                    (appointment["appointment_id"],)).fetchone():
        return False
    sql = ("SELECT 1 FROM scheduling_proposals WHERE (incoming_appointment_id=? OR affected_appointment_id=?) "
           f"AND proposal_status IN {OPEN_SQL}")
    params = [appointment["appointment_id"], appointment["appointment_id"]]
    if ignore_proposal_id is not None:
        sql += " AND proposal_id != ?"
        params.append(ignore_proposal_id)
    return conn.execute(sql, params).fetchone() is None


def _blocked_pair(conn, incoming, affected):
    # Do not recreate a rejected/declined/conflicting situation on every refresh.
    return conn.execute("SELECT 1 FROM scheduling_proposals WHERE incoming_appointment_id=? AND affected_appointment_id=? "
                        "AND incoming_version=? AND affected_version=? "
                        "AND proposal_status IN ('rejected','patient_declined','conflict')",
                        (incoming["appointment_id"], affected["appointment_id"], incoming["version"], affected["version"])).fetchone() is not None


def _freeable_target(conn, incoming, affected, clock):
    for target in _targets(conn, incoming, clock):
        occupants = _occupants(conn, incoming["doctor_id"], target, incoming["duration_minutes"])
        if len(occupants) == 1 and occupants[0]["appointment_id"] == affected["appointment_id"]:
            return target
    return None


def _find_candidates(conn, incoming, clock, max_candidates=5):
    if not _incoming_eligible(incoming):
        return []
    if any(_slot_available(conn, incoming["doctor_id"], target, incoming["duration_minutes"], clock)
           for target in _targets(conn, incoming, clock)):
        return []  # Even direct candidate queries prefer normal free-slot booking.
    first_day = to_local(incoming["requested_datetime"]).date()
    last_day = to_local(incoming["latest_acceptable_datetime"]).date()
    lower = to_storage(clinic_datetime(first_day, "00:00"))
    upper = to_storage(clinic_datetime(last_day + timedelta(days=1), "00:00"))
    rows = conn.execute("SELECT * FROM appointments WHERE doctor_id=? AND appointment_status='booked' "
                        "AND appointment_datetime>=? AND appointment_datetime<? AND priority_score<? "
                        "ORDER BY priority_score ASC,appointment_datetime,appointment_id",
                        (incoming["doctor_id"], lower, upper, incoming["priority_score"])).fetchall()
    candidates = []
    for row in rows:
        affected = db.row_to_dict(row)
        if not _routine_eligible(conn, affected, clock) or _blocked_pair(conn, incoming, affected):
            continue
        target = _freeable_target(conn, incoming, affected, clock)
        if target is None:
            continue
        after_source = as_utc(affected["appointment_datetime"]) + timedelta(minutes=affected["duration_minutes"])
        alternatives = _alternatives(conn, affected["doctor_id"], after_source, affected["duration_minutes"], clock,
                                     days=MAX_MOVE_DAYS, limit=1, exclude_appointment_id=affected["appointment_id"],
                                     avoid_interval=(target, incoming["duration_minutes"]))
        if not alternatives:
            continue
        reason = (f"Member 3 supplied priority {incoming['priority_score']:g} and action 'expedite'. "
                  "No free slot exists in the incoming patient's agreed window. "
                  f"Routine appointment #{affected['appointment_id']} (Member 3 score {affected['priority_score']:g}) "
                  "has an actual available alternative. Doctor approval and this patient's agreement are required. "
                  "This is a scheduling proposal, not a diagnosis.")
        candidates.append({"affected_appointment_id": affected["appointment_id"],
                           "affected_datetime": affected["appointment_datetime"],
                           "proposed_datetime": alternatives[0]["appointment_datetime"],
                           "incoming_target_datetime": to_storage(target),
                           "priority_score": affected["priority_score"], "reason": reason})
        if len(candidates) >= max_candidates:
            break
    return candidates


def find_rescheduling_candidates(incoming_appointment, *, db_path=None, now=None, max_candidates=5):
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or not 1 <= max_candidates <= 20:
        raise ValidationError("Candidate limit must be 1–20.")
    appointment_id = incoming_appointment["appointment_id"] if isinstance(incoming_appointment, dict) else incoming_appointment
    with db.read_connection(db_path) as conn:
        return _find_candidates(conn, db.required_row(conn, "appointments", appointment_id), utc_now(now), max_candidates)


def _create_proposal(conn, incoming_id, affected_id, proposed_datetime, reason, clock):
    reason = _reason(reason)
    incoming = db.required_row(conn, "appointments", incoming_id)
    affected = db.required_row(conn, "appointments", affected_id)
    proposed = validate_future_start(proposed_datetime, clock)
    existing = conn.execute(f"SELECT * FROM scheduling_proposals WHERE incoming_appointment_id=? AND proposal_status IN {OPEN_SQL}",
                            (incoming_id,)).fetchone()
    if existing:
        if existing["affected_appointment_id"] == affected_id and existing["proposed_datetime"] == to_storage(proposed):
            return dict(existing)
        raise ConflictError("This incoming appointment already has an active proposal.")
    if (not _incoming_eligible(incoming) or not _routine_eligible(conn, affected, clock)
            or incoming_id == affected_id or incoming["doctor_id"] != affected["doctor_id"]
            or affected["priority_score"] >= incoming["priority_score"] or _blocked_pair(conn, incoming, affected)):
        raise ValidationError("These appointments are not eligible for a routine rescheduling proposal.")
    if any(_slot_available(conn, incoming["doctor_id"], target, incoming["duration_minutes"], clock)
           for target in _targets(conn, incoming, clock)):
        raise ValidationError("An actual free slot exists in the patient's window; book normally instead of displacing anyone.")
    target = _freeable_target(conn, incoming, affected, clock)
    if target is None:
        raise ConflictError("Moving this appointment would not free a valid slot in the incoming patient's window.")
    source_end = as_utc(affected["appointment_datetime"]) + timedelta(minutes=affected["duration_minutes"])
    if proposed < source_end or to_local(proposed).date() > to_local(affected["appointment_datetime"]).date() + timedelta(days=MAX_MOVE_DAYS):
        raise ValidationError("The demo proposes later alternatives within seven clinic calendar days.")
    if (not _slot_available(conn, affected["doctor_id"], proposed, affected["duration_minutes"], clock, affected_id)
            or overlaps(proposed, affected["duration_minutes"], target, incoming["duration_minutes"])):
        raise SlotUnavailableError("The proposed alternative is not actually available or overlaps the incoming slot.")
    stamp = to_storage(clock)
    cursor = conn.execute(
        "INSERT INTO scheduling_proposals(incoming_appointment_id,affected_appointment_id,affected_original_datetime,"
        "proposed_datetime,incoming_target_datetime,incoming_version,affected_version,reason,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (incoming_id, affected_id, affected["appointment_datetime"], to_storage(proposed), to_storage(target),
         incoming["version"], affected["version"], reason, stamp, stamp))
    db.log_action(conn, incoming_id, "rescheduling_proposal_created", reason, proposal_id=cursor.lastrowid,
                  details={"affected_appointment_id": affected_id, "proposed_datetime": to_storage(proposed)}, now=clock)
    db.log_action(conn, affected_id, "rescheduling_proposal_received", "Original appointment remains intact until both parties agree and final checks succeed.",
                  proposal_id=cursor.lastrowid, now=clock)
    return db.required_row(conn, "scheduling_proposals", cursor.lastrowid)


def create_rescheduling_proposal(incoming_appointment_id, affected_appointment_id, proposed_datetime, reason, *, db_path=None, now=None):
    try:
        with db.transaction(db_path) as conn:
            return _create_proposal(conn, incoming_appointment_id, affected_appointment_id, proposed_datetime, reason, utc_now(now))
    except Exception as exc:
        _mutation_failure(exc, "Proposal creation failed", incoming_appointment_id, db_path=db_path, now=now)
        raise


def _result(conn, appointment_id, outcome, *, proposal=None, task=None, alternatives=None, error=None):
    return {"appointment": db.required_row(conn, "appointments", appointment_id), "outcome": outcome,
            "proposal": proposal, "task": task, "alternatives": alternatives or [], "error": error}


def _invalidate_proposals(conn, appointment_id, reason, clock):
    rows = conn.execute(f"SELECT * FROM scheduling_proposals WHERE (incoming_appointment_id=? OR affected_appointment_id=?) "
                        f"AND proposal_status IN {OPEN_SQL}", (appointment_id, appointment_id)).fetchall()
    for proposal in rows:
        conn.execute("UPDATE scheduling_proposals SET proposal_status='invalidated',response_reason=?,updated_at=? WHERE proposal_id=?",
                     (reason, to_storage(clock), proposal["proposal_id"]))
        db.log_action(conn, appointment_id, "proposal_invalidated", reason, proposal_id=proposal["proposal_id"], now=clock)
        incoming = db.required_row(conn, "appointments", proposal["incoming_appointment_id"])
        if incoming["appointment_status"] == "requested":
            db.ensure_staff_task(conn, incoming["appointment_id"], "proposal_invalidated", reason, now=clock)


def optimize_appointment(appointment_id, *, db_path=None, now=None):
    """Preserve/allocate normal slots before considering any displacement proposal."""
    clock = utc_now(now)
    try:
        with db.transaction(db_path) as conn:
            appointment = db.required_row(conn, "appointments", appointment_id)
            if appointment["appointment_status"] in ("completed", "cancelled"):
                skip_pending_in_transaction(conn, appointment_id, "Appointment is inactive.", now=clock)
                return _result(conn, appointment_id, "not_active")
            if appointment["evaluation_status"] != "ready":
                task = db.ensure_staff_task(conn, appointment_id, "priority_not_ready", "Priority result is not ready; scheduling needs staff review.", now=clock)
                return _result(conn, appointment_id, "staff_review", task=task)
            if _requires_staff(appointment):
                if appointment["appointment_status"] == "booked":
                    sync_reminders_in_transaction(conn, appointment, now=clock)
                task = db.ensure_staff_task(conn, appointment_id, "clinical_review",
                                            "Member 3 requested human review or returned urgency/risk flags. No automatic displacement is permitted.", now=clock)
                return _result(conn, appointment_id, "staff_review", task=task)
            if appointment["appointment_status"] == "booked":
                sync_reminders_in_transaction(conn, appointment, now=clock)
                return _result(conn, appointment_id, "unchanged")
            _doctor(conn, appointment["doctor_id"])
            for target in _targets(conn, appointment, clock):
                if _slot_available(conn, appointment["doctor_id"], target, appointment["duration_minutes"], clock):
                    conn.execute("UPDATE appointments SET appointment_datetime=?,appointment_status='booked',confirmed=1,"
                                 "version=version+1,updated_at=? WHERE appointment_id=?",
                                 (to_storage(target), to_storage(clock), appointment_id))
                    _invalidate_proposals(conn, appointment_id, "A normal free slot became available; no displacement is necessary.", clock)
                    updated = db.required_row(conn, "appointments", appointment_id)
                    sync_reminders_in_transaction(conn, updated, now=clock)
                    db.resolve_tasks(conn, appointment_id, ("no_suitable_slot", "proposal_conflict", "priority_not_ready", "proposal_invalidated"),
                                     "Booked normally into a free slot.", now=clock)
                    db.log_action(conn, appointment_id, "appointment_booked", "An actual free slot in the patient's agreed window was booked without displacement.", now=clock)
                    return _result(conn, appointment_id, "booked")
            existing = conn.execute(f"SELECT * FROM scheduling_proposals WHERE incoming_appointment_id=? AND proposal_status IN {OPEN_SQL}",
                                    (appointment_id,)).fetchone()
            if existing:
                return _result(conn, appointment_id, "proposal_created", proposal=dict(existing))
            candidates = _find_candidates(conn, appointment, clock)
            if candidates:
                choice = candidates[0]
                proposal = _create_proposal(conn, appointment_id, choice["affected_appointment_id"],
                                            choice["proposed_datetime"], choice["reason"], clock)
                return _result(conn, appointment_id, "proposal_created", proposal=proposal)
            task = db.ensure_staff_task(conn, appointment_id, "no_suitable_slot",
                                        "No free slot exists in the patient's window and no safe, eligible routine move was found. Staff must review; no patient was displaced.", now=clock)
            alternatives = _alternatives(conn, appointment["doctor_id"], as_utc(appointment["requested_datetime"]),
                                         appointment["duration_minutes"], clock)
            return _result(conn, appointment_id, "staff_review", task=task, alternatives=alternatives)
    except Exception as exc:
        _mutation_failure(exc, "Scheduling optimization failed", appointment_id, db_path=db_path, now=clock)
        raise


def _proposal_doctor(conn, proposal, doctor_id):
    incoming = db.required_row(conn, "appointments", proposal["incoming_appointment_id"])
    affected = db.required_row(conn, "appointments", proposal["affected_appointment_id"])
    if not doctor_id or incoming["doctor_id"] != doctor_id or affected["doctor_id"] != doctor_id:
        raise AuthorizationError("Only the assigned doctor can respond to this proposal.")
    return incoming, affected


def _consent_status(doctor_response, patient_response):
    if doctor_response == "approved" and patient_response == "agreed":
        return "ready"
    if doctor_response == "approved":
        return "doctor_approved"
    if patient_response == "agreed":
        return "patient_agreed"
    return "pending"


def approve_proposal(proposal_id, doctor_id, *, db_path=None, now=None):
    try:
        with db.transaction(db_path) as conn:
            proposal = db.required_row(conn, "scheduling_proposals", proposal_id)
            _proposal_doctor(conn, proposal, doctor_id)
            if proposal["doctor_response"] == "approved" and proposal["proposal_status"] in (*db.OPEN_PROPOSAL_STATES, "confirmed"):
                return proposal
            if proposal["proposal_status"] not in db.OPEN_PROPOSAL_STATES:
                raise ConflictError("This proposal is no longer active.")
            status = _consent_status("approved", proposal["patient_response"])
            conn.execute("UPDATE scheduling_proposals SET doctor_response='approved',proposal_status=?,updated_at=? WHERE proposal_id=?",
                         (status, to_storage(utc_now(now)), proposal_id))
            db.log_action(conn, proposal["incoming_appointment_id"], "proposal_approved", "Doctor approved the proposed time; patient agreement is still independently required.",
                          f"doctor:{doctor_id}", proposal_id=proposal_id, now=now)
            return db.required_row(conn, "scheduling_proposals", proposal_id)
    except Exception as exc:
        _mutation_failure(exc, "Proposal approval failed", actor=f"doctor:{doctor_id}", db_path=db_path, now=now)
        raise


def reject_proposal(proposal_id, doctor_id, reason, *, db_path=None, now=None):
    reason = _reason(reason)
    try:
        with db.transaction(db_path) as conn:
            proposal = db.required_row(conn, "scheduling_proposals", proposal_id)
            _proposal_doctor(conn, proposal, doctor_id)
            if proposal["proposal_status"] == "rejected":
                return proposal
            if proposal["proposal_status"] not in db.OPEN_PROPOSAL_STATES:
                raise ConflictError("A terminal/confirmed proposal cannot be rejected.")
            conn.execute("UPDATE scheduling_proposals SET doctor_response='rejected',proposal_status='rejected',response_reason=?,updated_at=? WHERE proposal_id=?",
                         (reason, to_storage(utc_now(now)), proposal_id))
            db.log_action(conn, proposal["incoming_appointment_id"], "proposal_rejected", reason, f"doctor:{doctor_id}", proposal_id=proposal_id, now=now)
            db.ensure_staff_task(conn, proposal["incoming_appointment_id"], "proposal_rejected", "Doctor rejected the proposal. Original appointments are unchanged.", now=now)
            return db.required_row(conn, "scheduling_proposals", proposal_id)
    except Exception as exc:
        _mutation_failure(exc, "Proposal rejection failed", actor=f"doctor:{doctor_id}", db_path=db_path, now=now)
        raise


def record_patient_response(proposal_id, patient_id, agree=True, *, db_path=None, now=None):
    if not isinstance(agree, bool):
        raise ValidationError("Patient agreement must be a boolean.")
    try:
        with db.transaction(db_path) as conn:
            proposal = db.required_row(conn, "scheduling_proposals", proposal_id)
            affected = db.required_row(conn, "appointments", proposal["affected_appointment_id"])
            if not patient_id or affected["patient_id"] != patient_id:
                raise AuthorizationError("Only the affected patient can agree to this proposed change.")
            response = "agreed" if agree else "declined"
            if proposal["patient_response"] == response and proposal["proposal_status"] in (*db.OPEN_PROPOSAL_STATES, "confirmed", "patient_declined"):
                return proposal
            if proposal["proposal_status"] not in db.OPEN_PROPOSAL_STATES:
                raise ConflictError("This proposal is no longer active.")
            status = _consent_status(proposal["doctor_response"], response) if agree else "patient_declined"
            conn.execute("UPDATE scheduling_proposals SET patient_response=?,proposal_status=?,updated_at=? WHERE proposal_id=?",
                         (response, status, to_storage(utc_now(now)), proposal_id))
            db.log_action(conn, affected["appointment_id"], "patient_confirmation_received" if agree else "patient_declined_proposal",
                          "Patient explicitly agreed to the stored proposed time." if agree else "Patient declined; the original appointment is unchanged.",
                          f"patient:{patient_id}", proposal_id=proposal_id, now=now)
            if not agree:
                db.ensure_staff_task(conn, proposal["incoming_appointment_id"], "proposal_declined", "Affected patient declined the proposed change; staff review is required.", now=now)
            return db.required_row(conn, "scheduling_proposals", proposal_id)
    except Exception as exc:
        _mutation_failure(exc, "Patient proposal response failed", actor=f"patient:{patient_id}", db_path=db_path, now=now)
        raise


def _confirmation_problem(conn, proposal, incoming, affected, clock):
    if incoming["version"] != proposal["incoming_version"] or affected["version"] != proposal["affected_version"]:
        return "An appointment changed after the proposal was created; fresh review/consent is required."
    if (not _incoming_eligible(incoming) or not _routine_eligible(conn, affected, clock, proposal["proposal_id"])
            or affected["priority_score"] >= incoming["priority_score"]):
        return "The incoming or affected appointment is no longer eligible; urgent/protected visits cannot be displaced."
    if affected["appointment_datetime"] != proposal["affected_original_datetime"]:
        return "The affected appointment no longer matches the original agreed record."
    try:
        _doctor(conn, incoming["doctor_id"])
        proposed = validate_future_start(proposal["proposed_datetime"], clock)
        target = validate_future_start(proposal["incoming_target_datetime"], clock)
        if not as_utc(incoming["requested_datetime"]) <= target <= as_utc(incoming["latest_acceptable_datetime"]):
            return "The incoming slot is outside the patient's requested window."
        if not _slot_available(conn, affected["doctor_id"], proposed, affected["duration_minutes"], clock, affected["appointment_id"]):
            return "The proposed alternative is no longer available. Neither appointment was changed."
        if not _slot_available(conn, incoming["doctor_id"], target, incoming["duration_minutes"], clock, affected["appointment_id"]):
            return "The incoming target is no longer available after excluding the affected visit."
        if overlaps(proposed, affected["duration_minutes"], target, incoming["duration_minutes"]):
            return "The two proposed appointment intervals overlap."
    except HealthcareError as exc:
        return str(exc)
    return None


def _proposal_conflict(conn, proposal, reason, clock):
    conn.execute("UPDATE scheduling_proposals SET proposal_status='conflict',response_reason=?,updated_at=? WHERE proposal_id=?",
                 (reason, to_storage(clock), proposal["proposal_id"]))
    db.log_action(conn, proposal["incoming_appointment_id"], "rescheduling_conflict", reason,
                  proposal_id=proposal["proposal_id"], now=clock)
    db.ensure_staff_task(conn, proposal["incoming_appointment_id"], "proposal_conflict", reason, now=clock)
    return {"outcome": "conflict", "proposal": db.required_row(conn, "scheduling_proposals", proposal["proposal_id"]),
            "incoming_appointment": db.required_row(conn, "appointments", proposal["incoming_appointment_id"]),
            "affected_appointment": db.required_row(conn, "appointments", proposal["affected_appointment_id"]), "error": reason}


def confirm_rescheduling(proposal_id, doctor_id, *, db_path=None, now=None):
    clock = utc_now(now)
    try:
        with db.transaction(db_path) as conn:
            proposal = db.required_row(conn, "scheduling_proposals", proposal_id)
            incoming, affected = _proposal_doctor(conn, proposal, doctor_id)
            if proposal["proposal_status"] == "confirmed":
                return {"outcome": "confirmed", "proposal": proposal, "incoming_appointment": incoming,
                        "affected_appointment": affected, "error": None}
            if proposal["proposal_status"] not in db.OPEN_PROPOSAL_STATES:
                raise ConflictError("This proposal is not active; create a fresh reviewed proposal if appropriate.")
            if proposal["doctor_response"] != "approved" or proposal["patient_response"] != "agreed":
                raise ApprovalRequiredError("No appointment changed: BOTH doctor approval and affected-patient agreement are required.")
            problem = _confirmation_problem(conn, proposal, incoming, affected, clock)
            if problem:
                return _proposal_conflict(conn, proposal, problem, clock)
            # A savepoint additionally guarantees no half-change if a constraint fails
            # during either UPDATE, reminder insertion, or audit insertion.
            conn.execute("SAVEPOINT apply_schedule")
            try:
                stamp = to_storage(clock)
                conn.execute("UPDATE appointments SET appointment_datetime=?,version=version+1,updated_at=? WHERE appointment_id=?",
                             (proposal["proposed_datetime"], stamp, affected["appointment_id"]))
                conn.execute("UPDATE appointments SET appointment_datetime=?,appointment_status='booked',confirmed=1,"
                             "version=version+1,updated_at=? WHERE appointment_id=?",
                             (proposal["incoming_target_datetime"], stamp, incoming["appointment_id"]))
                conn.execute("UPDATE scheduling_proposals SET proposal_status='confirmed',confirmed_at=?,updated_at=? WHERE proposal_id=?",
                             (stamp, stamp, proposal_id))
                updated_affected = db.required_row(conn, "appointments", affected["appointment_id"])
                updated_incoming = db.required_row(conn, "appointments", incoming["appointment_id"])
                sync_reminders_in_transaction(conn, updated_affected, now=clock)
                sync_reminders_in_transaction(conn, updated_incoming, now=clock)
                db.resolve_tasks(conn, incoming["appointment_id"], ("no_suitable_slot", "proposal_conflict", "proposal_rejected", "proposal_declined", "proposal_invalidated"),
                                 "Two-party rescheduling successfully confirmed.", now=clock)
                db.log_action(conn, affected["appointment_id"], "appointment_rescheduled", proposal["reason"], f"doctor:{doctor_id}",
                              proposal_id=proposal_id, details={"old": affected["appointment_datetime"], "new": proposal["proposed_datetime"]}, now=clock)
                db.log_action(conn, incoming["appointment_id"], "proposal_confirmed", "Both parties agreed; both intervals were revalidated and booked atomically.",
                              f"doctor:{doctor_id}", proposal_id=proposal_id, now=clock)
                conn.execute("RELEASE apply_schedule")
            except sqlite3.IntegrityError as exc:
                conn.execute("ROLLBACK TO apply_schedule")
                conn.execute("RELEASE apply_schedule")
                return _proposal_conflict(conn, proposal, f"Final database constraint conflict: {exc}. Both appointments remain unchanged.", clock)
            return {"outcome": "confirmed", "proposal": db.required_row(conn, "scheduling_proposals", proposal_id),
                    "incoming_appointment": updated_incoming, "affected_appointment": updated_affected, "error": None}
    except Exception as exc:
        _mutation_failure(exc, "Rescheduling confirmation failed", actor=f"doctor:{doctor_id}", db_path=db_path, now=clock)
        raise


def _close_patient_requests(conn, appointment_id, reason, clock):
    rows = conn.execute("SELECT request_id FROM appointment_requests WHERE appointment_id=? AND request_status='pending'",
                        (appointment_id,)).fetchall()
    for row in rows:
        conn.execute("UPDATE appointment_requests SET request_status='invalidated',doctor_response=?,updated_at=? WHERE request_id=?",
                     (reason, to_storage(clock), row[0]))
        db.log_action(conn, appointment_id, "patient_request_invalidated", reason, details={"request_id": row[0]}, now=clock)


def cancel_appointment(appointment_id, patient_id, reason="Patient requested cancellation.", *, db_path=None, now=None):
    clock = utc_now(now)
    try:
        reason = _reason(reason)
        with db.transaction(db_path) as conn:
            appointment = db.required_row(conn, "appointments", appointment_id)
            if not patient_id or appointment["patient_id"] != patient_id:
                raise AuthorizationError("Only the owner patient can cancel this appointment through this interface.")
            if appointment["appointment_status"] == "cancelled":
                return appointment
            if appointment["appointment_status"] == "completed":
                raise ConflictError("A completed appointment cannot be cancelled.")
            conn.execute("UPDATE appointments SET appointment_status='cancelled',confirmed=0,version=version+1,updated_at=? WHERE appointment_id=?",
                         (to_storage(clock), appointment_id))
            skip_pending_in_transaction(conn, appointment_id, "Patient cancelled the appointment.", now=clock)
            _invalidate_proposals(conn, appointment_id, "An appointment in the proposal was cancelled by its patient.", clock)
            _close_patient_requests(conn, appointment_id, "Patient already cancelled directly.", clock)
            db.resolve_tasks(conn, appointment_id, ("no_suitable_slot", "priority_not_ready", "priority_engine_error", "proposal_conflict", "proposal_rejected", "proposal_declined"),
                             "Appointment cancelled by its patient.", now=clock)
            db.log_action(conn, appointment_id, "appointment_cancelled", reason, f"patient:{patient_id}", now=clock)
            return db.required_row(conn, "appointments", appointment_id)
    except Exception as exc:
        _mutation_failure(exc, "Cancellation failed", appointment_id, f"patient:{patient_id}", db_path=db_path, now=clock)
        raise


def complete_appointment(appointment_id, doctor_id, *, db_path=None, now=None):
    clock = utc_now(now)
    try:
        with db.transaction(db_path) as conn:
            appointment = db.required_row(conn, "appointments", appointment_id)
            if appointment["doctor_id"] != doctor_id:
                raise AuthorizationError("Only the assigned doctor can complete this appointment.")
            if appointment["appointment_status"] == "completed":
                return appointment
            if appointment["appointment_status"] != "booked" or as_utc(appointment["appointment_datetime"]) > clock:
                raise ValidationError("Only an active appointment that has started can be marked completed.")
            conn.execute("UPDATE appointments SET appointment_status='completed',confirmed=0,version=version+1,updated_at=? WHERE appointment_id=?",
                         (to_storage(clock), appointment_id))
            skip_pending_in_transaction(conn, appointment_id, "Appointment completed.", now=clock)
            _invalidate_proposals(conn, appointment_id, "An appointment in this proposal was completed.", clock)
            _close_patient_requests(conn, appointment_id, "Appointment completed.", clock)
            db.log_action(conn, appointment_id, "appointment_completed", "Doctor marked the appointment completed.", f"doctor:{doctor_id}", now=clock)
            return db.required_row(conn, "appointments", appointment_id)
    except Exception as exc:
        _mutation_failure(exc, "Completion failed", appointment_id, f"doctor:{doctor_id}", db_path=db_path, now=clock)
        raise


def request_patient_change(appointment_id, patient_id, request_type, requested_datetime=None,
                           reason="Patient requested a change.", *, db_path=None, now=None):
    clock = utc_now(now)
    try:
        reason = _reason(reason)
        if request_type not in ("cancellation", "reschedule"):
            raise ValidationError("Request type must be cancellation or reschedule.")
        with db.transaction(db_path) as conn:
            appointment = db.required_row(conn, "appointments", appointment_id)
            if not patient_id or appointment["patient_id"] != patient_id:
                raise AuthorizationError("Only the owner patient can submit this request.")
            if appointment["appointment_status"] != "booked" or as_utc(appointment["appointment_datetime"]) <= clock:
                raise ValidationError("Change requests require a future booked appointment.")
            target = None
            if request_type == "reschedule":
                _doctor(conn, appointment["doctor_id"])
                target = validate_future_start(requested_datetime, clock)
                if target == as_utc(appointment["appointment_datetime"]):
                    raise ValidationError("Choose a different appointment time.")
                if not _fits_availability(conn, appointment["doctor_id"], target, appointment["duration_minutes"]):
                    raise ValidationError("Requested alternative must fit the doctor's predefined slot grid.")
            old = conn.execute("SELECT * FROM appointment_requests WHERE appointment_id=? AND request_status='pending'", (appointment_id,)).fetchone()
            if old:
                if old["request_type"] == request_type and old["requested_datetime"] == (to_storage(target) if target else None):
                    return dict(old)
                raise ConflictError("There is already a pending change request for this appointment.")
            stamp = to_storage(clock)
            conn.execute("UPDATE appointments SET version=version+1,updated_at=? WHERE appointment_id=?", (stamp, appointment_id))
            version = appointment["version"] + 1
            cursor = conn.execute("INSERT INTO appointment_requests(appointment_id,patient_id,request_type,requested_datetime,"
                                  "appointment_version,patient_agreed,reason,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?,?)",
                                  (appointment_id, patient_id, request_type, to_storage(target) if target else None, version, reason, stamp, stamp))
            _invalidate_proposals(conn, appointment_id, "Patient submitted their own change request; earlier proposed consent is no longer applicable.", clock)
            db.log_action(conn, appointment_id, "patient_change_requested", reason, f"patient:{patient_id}",
                          details={"request_id": cursor.lastrowid, "type": request_type}, now=clock)
            return db.required_row(conn, "appointment_requests", cursor.lastrowid)
    except Exception as exc:
        _mutation_failure(exc, "Patient change request failed", appointment_id, f"patient:{patient_id}", db_path=db_path, now=clock)
        raise


def review_patient_request(request_id, doctor_id, approve, reason="Doctor reviewed the patient request.", *, db_path=None, now=None):
    clock = utc_now(now)
    try:
        if not isinstance(approve, bool):
            raise ValidationError("Approval must be a boolean.")
        reason = _reason(reason)
        with db.transaction(db_path) as conn:
            request = db.required_row(conn, "appointment_requests", request_id)
            appointment = db.required_row(conn, "appointments", request["appointment_id"])
            if not doctor_id or appointment["doctor_id"] != doctor_id:
                raise AuthorizationError("Only the assigned doctor can review this request.")
            if request["request_status"] != "pending":
                return {"outcome": request["request_status"], "request": request, "appointment": appointment, "error": None}
            problem = None
            if approve:
                if (appointment["appointment_status"] != "booked" or appointment["version"] != request["appointment_version"]
                        or as_utc(appointment["appointment_datetime"]) <= clock):
                    problem = "Appointment changed or started after the request; fresh review is needed."
                elif request["request_type"] == "reschedule":
                    _doctor(conn, appointment["doctor_id"])
                    target = validate_future_start(request["requested_datetime"], clock)
                    if not request["patient_agreed"] or not _slot_available(conn, appointment["doctor_id"], target, appointment["duration_minutes"], clock, appointment["appointment_id"]):
                        problem = "Requested alternative is not available/consented to. Original booking is unchanged."
            status = "conflict" if problem else ("approved" if approve else "rejected")
            if approve and not problem:
                if request["request_type"] == "cancellation":
                    conn.execute("UPDATE appointments SET appointment_status='cancelled',confirmed=0,version=version+1,updated_at=? WHERE appointment_id=?",
                                 (to_storage(clock), appointment["appointment_id"]))
                    skip_pending_in_transaction(conn, appointment["appointment_id"], "Patient cancellation request approved.", now=clock)
                else:
                    conn.execute("UPDATE appointments SET appointment_datetime=?,version=version+1,updated_at=? WHERE appointment_id=?",
                                 (request["requested_datetime"], to_storage(clock), appointment["appointment_id"]))
                    sync_reminders_in_transaction(conn, db.required_row(conn, "appointments", appointment["appointment_id"]), now=clock)
                _invalidate_proposals(conn, appointment["appointment_id"], "Patient's own change request was approved.", clock)
                db.log_action(conn, appointment["appointment_id"], "appointment_cancelled" if request["request_type"] == "cancellation" else "appointment_rescheduled",
                              reason, f"doctor:{doctor_id}", details={"patient_request_id": request_id}, now=clock)
            conn.execute("UPDATE appointment_requests SET request_status=?,doctor_response=?,updated_at=? WHERE request_id=?",
                         (status, problem or reason, to_storage(clock), request_id))
            db.log_action(conn, appointment["appointment_id"], "patient_request_reviewed", problem or reason, f"doctor:{doctor_id}",
                          details={"request_id": request_id, "status": status}, now=clock)
            if problem:
                db.ensure_staff_task(conn, appointment["appointment_id"], "patient_request_conflict", problem, now=clock)
            return {"outcome": status, "request": db.required_row(conn, "appointment_requests", request_id),
                    "appointment": db.required_row(conn, "appointments", appointment["appointment_id"]), "error": problem}
    except Exception as exc:
        _mutation_failure(exc, "Patient request review failed", actor=f"doctor:{doctor_id}", db_path=db_path, now=clock)
        raise
