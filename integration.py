"""Stable service façade for Members 1–3. Dashboards share these DB-backed APIs.

Member 4 backend handoff: no Streamlit/dashboard or mock-engine dependency.
No cached state, circular dashboard imports, or competing priority model. Caller
identities must come from Members 1/2's authenticated sessions. Pass Member 3's
actual adapter via engine=, or provide priority_engine.evaluate_priority.
"""
from collections.abc import Mapping
import json
import math
from numbers import Real
import sqlite3

import database as db
from errors import AuthorizationError, ConflictError, PriorityEngineError, ValidationError
from importlib import import_module
import scheduler
from time_utils import to_storage, utc_now

# Public shared operations. Keep names/signatures identical for both team dashboards.
from scheduler import (approve_proposal, cancel_appointment, complete_appointment,
                       confirm_rescheduling, create_rescheduling_proposal,
                       find_alternative_slots, find_rescheduling_candidates,
                       get_available_slots, is_slot_available, reject_proposal,
                       review_patient_request)
from reminder_manager import (get_reminder_history, process_due_reminders, schedule_reminders,
                              sync_reminders_in_transaction)


def _resolve_priority_engine(engine):
    """Use the team's engine; do not require or ship a mock scoring module."""
    if engine is not None:
        if not callable(engine):
            raise PriorityEngineError("engine= must be Member 3's callable adapter.")
        return engine
    try:
        provider = import_module("priority_engine")
    except ImportError as exc:
        raise PriorityEngineError(
            f"Member 3 engine is unavailable: {exc}. Pass the actual adapter via engine=.") from exc
    candidate = getattr(provider, "evaluate_priority", None)
    if not callable(candidate):
        raise PriorityEngineError(
            "Member 3 must expose evaluate_priority(appointment, patient), or pass its adapter via engine=.")
    return candidate

SUPPORTED_ACTIONS = {"routine_booking", "expedite", "escalate_to_staff", "emergency_review"}
ACTION_URGENCY = {"routine_booking": "routine", "expedite": "priority",
                  "escalate_to_staff": "urgent", "emergency_review": "emergency"}


def validate_priority_result(raw):
    """Adapt Member 3 here if field/action names differ. Validate, do not rescore."""
    if not isinstance(raw, Mapping):
        raise PriorityEngineError("Member 3 must return a mapping/dictionary.")
    needed = {"priority_score", "risk_flags", "reason", "recommended_action"}
    missing = needed - raw.keys()
    if missing:
        raise PriorityEngineError("Member 3 result is missing: " + ", ".join(sorted(missing)))
    score = raw["priority_score"]
    if isinstance(score, bool) or not isinstance(score, Real) or not math.isfinite(score) or not 0 <= score <= 100:
        raise PriorityEngineError("Member 3 priority_score must be a finite number from 0 to 100.")
    flags = raw["risk_flags"]
    if (not isinstance(flags, list) or len(flags) > 50
            or any(not isinstance(f, str) or not f.strip() or len(f) > 200 for f in flags)):
        raise PriorityEngineError("Member 3 risk_flags must be a list of short, nonempty strings.")
    reason = raw["reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
        raise PriorityEngineError("Member 3 reason must contain 1–4000 characters.")
    action = raw["recommended_action"]
    if not isinstance(action, str) or action not in SUPPORTED_ACTIONS:
        raise PriorityEngineError("Map Member 3's recommended_action to routine_booking, expedite, escalate_to_staff, or emergency_review.")
    urgency = raw.get("urgency", ACTION_URGENCY[action])
    if not isinstance(urgency, str) or urgency not in {"routine", "priority", "urgent", "emergency"}:
        raise PriorityEngineError("Member 3 urgency must be routine, priority, urgent, or emergency.")
    source = raw.get("source", "member3-adapter")
    if not isinstance(source, str) or not source.strip() or len(source) > 300:
        raise PriorityEngineError("Engine source must be a short nonempty string.")
    return {"priority_score": float(score), "risk_flags": [f.strip() for f in flags],
            "reason": reason.strip(), "recommended_action": action, "urgency": urgency, "source": source}


def _processing_result(appointment, outcome, *, task=None, error=None):
    return {"appointment": appointment, "outcome": outcome, "proposal": None,
            "task": task, "alternatives": [], "error": error}


def _engine_failure(appointment_id, snapshot_version, exc, db_path, clock):
    reason = f"Priority engine failed: {type(exc).__name__}: {exc}"[:4000]
    with db.transaction(db_path) as conn:
        current = db.required_row(conn, "appointments", appointment_id)
        db.log_action(conn, appointment_id, "priority_engine_failed", reason, now=clock)
        if current["appointment_status"] in ("cancelled", "completed"):
            return _processing_result(current, "not_active", error=reason)
        if current["version"] != snapshot_version:
            return _processing_result(current, "conflict", error="Stale engine failure ignored because the appointment changed. Refresh and retry.")
        # Keep booking, last score, explanation and reminder history. Mark the latest
        # evaluation error so this record cannot be treated as a routine candidate.
        conn.execute("UPDATE appointments SET evaluation_status='error',engine_error=?,updated_at=?,version=version+1 WHERE appointment_id=?",
                     (reason, to_storage(clock), appointment_id))
        scheduler._invalidate_proposals(conn, appointment_id, "Priority evaluation failed; earlier scheduling consent needs fresh review.", clock)
        task = db.ensure_staff_task(conn, appointment_id, "priority_engine_error", reason, now=clock)
        return _processing_result(db.required_row(conn, "appointments", appointment_id), "engine_error", task=task, error=reason)


def process_new_appointment(appointment_id, *, engine=None, force=False, db_path=None, now=None):
    """Evaluate once, persist Member 3, then allocate/preserve/propose/refer.

The engine call is outside a write transaction. An optimistic version check rejects
stale results. Re-running this function does not duplicate active proposals/reminders.
`force=True` is an explicit reevaluation and therefore changes the version/consent.
"""
    if not isinstance(force, bool):
        raise ValidationError("force must be a boolean.")
    clock = utc_now(now)
    try:
        with db.read_connection(db_path) as conn:
            snapshot = db.required_row(conn, "appointments", appointment_id)
            patient = db.required_row(conn, "patients", snapshot["patient_id"])
        if snapshot["appointment_status"] in ("cancelled", "completed"):
            return scheduler.optimize_appointment(appointment_id, db_path=db_path, now=clock)
        needs_evaluation = force or snapshot["evaluation_status"] != "ready"
        if needs_evaluation:
            try:
                raw = _resolve_priority_engine(engine)(snapshot.copy(), patient.copy())
                result = validate_priority_result(raw)
            except Exception as exc:
                return _engine_failure(appointment_id, snapshot["version"], exc, db_path, clock)
            with db.transaction(db_path) as conn:
                current = db.required_row(conn, "appointments", appointment_id)
                if current["version"] != snapshot["version"]:
                    raise ConflictError("Appointment changed while Member 3 was evaluating. Stale result was not saved; refresh and retry.")
                if current["appointment_status"] not in ("requested", "booked"):
                    return _processing_result(current, "not_active")
                conn.execute(
                    "UPDATE appointments SET priority_score=?,risk_flags=?,agent_reason=?,recommended_action=?,urgency=?,"
                    "evaluation_status='ready',priority_evaluated_at=?,engine_source=?,engine_error=NULL,"
                    "version=version+1,updated_at=? WHERE appointment_id=?",
                    (result["priority_score"], json.dumps(result["risk_flags"]), result["reason"], result["recommended_action"],
                     result["urgency"], to_storage(clock), result["source"], to_storage(clock), appointment_id))
                if force:
                    scheduler._invalidate_proposals(conn, appointment_id, "Member 3 result was explicitly reevaluated; new consent is required.", clock)
                db.log_action(conn, appointment_id, "priority_result_saved", result["reason"], "member3-adapter",
                              details={"score": result["priority_score"], "urgency": result["urgency"],
                                       "action": result["recommended_action"], "source": result["source"]}, now=clock)
                db.resolve_tasks(conn, appointment_id, ("priority_engine_error", "priority_not_ready"), "A validated Member 3 result is now available.", now=clock)
        processed = scheduler.optimize_appointment(appointment_id, db_path=db_path, now=clock)
        if needs_evaluation:
            with db.transaction(db_path) as conn:
                db.log_action(conn, appointment_id, "new_appointment_processed", f"Integration outcome: {processed['outcome']}.", now=clock)
        return processed
    except ConflictError as exc:
        db.log_failure("operation_failed", str(exc), appointment_id=appointment_id, db_path=db_path, now=clock)
        return _processing_result(db.get_appointment(appointment_id, db_path), "conflict", error=str(exc))
    except Exception as exc:
        db.log_failure("operation_failed", f"Appointment processing failed: {exc}", appointment_id=appointment_id, db_path=db_path, now=clock)
        # Read/schema/database errors are not mislabeled as an engine failure.
        raise


def book_appointment(patient_id, doctor_id, appointment_datetime, duration_minutes=None, *, engine=None, db_path=None, now=None):
    appointment = scheduler.book_appointment(patient_id, doctor_id, appointment_datetime, duration_minutes, db_path=db_path, now=now)
    return process_new_appointment(appointment["appointment_id"], engine=engine, db_path=db_path, now=now)


def submit_appointment_request(patient_id, doctor_id, appointment_datetime, duration_minutes=None,
                               latest_acceptable_datetime=None, *, db_path=None, now=None):
    return scheduler.create_appointment_request(patient_id, doctor_id, appointment_datetime, duration_minutes,
                                                latest_acceptable_datetime, db_path=db_path, now=now)


def respond_to_proposal(proposal_id, patient_id, agree=True, *, db_path=None, now=None):
    return scheduler.record_patient_response(proposal_id, patient_id, agree, db_path=db_path, now=now)


def request_cancellation(appointment_id, patient_id, reason="Patient requested cancellation.", *, db_path=None, now=None):
    return scheduler.request_patient_change(appointment_id, patient_id, "cancellation", reason=reason, db_path=db_path, now=now)


def request_rescheduling(appointment_id, patient_id, proposed_datetime, reason="Patient requested another time.", *, db_path=None, now=None):
    return scheduler.request_patient_change(appointment_id, patient_id, "reschedule", proposed_datetime, reason, db_path=db_path, now=now)


def get_patient_dashboard_data(patient_id, *, db_path=None):
    """Consistent snapshot, scoped to this patient; never includes another patient's profile."""
    with db.read_connection(db_path) as conn:
        patient = db.required_row(conn, "patients", patient_id)
        appointments = [db.row_to_dict(r) for r in conn.execute(
            "SELECT a.*,d.display_name AS doctor_name FROM appointments a JOIN doctors d ON d.doctor_id=a.doctor_id "
            "WHERE a.patient_id=? ORDER BY a.appointment_datetime,a.appointment_id", (patient_id,))]
        reminders = [dict(r) for r in conn.execute(
            "SELECT r.* FROM reminders r JOIN appointments a ON a.appointment_id=r.appointment_id "
            "WHERE a.patient_id=? ORDER BY r.scheduled_at DESC,r.reminder_id DESC", (patient_id,))]
        proposals = [dict(r) for r in conn.execute(
            "SELECT sp.proposal_id,sp.affected_appointment_id,sp.affected_original_datetime,sp.proposed_datetime,"
            "sp.proposal_status,sp.doctor_response,sp.patient_response,sp.created_at,sp.updated_at, a.doctor_id "
            "FROM scheduling_proposals sp JOIN appointments a ON a.appointment_id=sp.affected_appointment_id "
            "WHERE a.patient_id=? ORDER BY sp.proposal_id DESC", (patient_id,))]
        for proposal in proposals:
            proposal["reason"] = "A clinician requested a schedule adjustment. Your original booking stays intact until you agree and final checks succeed."
        requests = [dict(r) for r in conn.execute(
            "SELECT ar.* FROM appointment_requests ar JOIN appointments a ON a.appointment_id=ar.appointment_id "
            "WHERE a.patient_id=? ORDER BY ar.request_id DESC", (patient_id,))]
        return {"patient": patient, "appointments": appointments, "reminders": reminders, "proposals": proposals, "requests": requests}


def get_doctor_dashboard_data(doctor_id, *, db_path=None):
    with db.read_connection(db_path) as conn:
        doctor = db.required_row(conn, "doctors", doctor_id)
        appointments = [db.row_to_dict(r) for r in conn.execute(
            "SELECT a.*,p.display_name AS patient_name FROM appointments a JOIN patients p ON p.patient_id=a.patient_id "
            "WHERE a.doctor_id=? ORDER BY CASE WHEN a.appointment_status IN ('requested','booked') THEN 0 ELSE 1 END,"
            "CASE a.urgency WHEN 'emergency' THEN 0 WHEN 'urgent' THEN 1 WHEN 'priority' THEN 2 ELSE 3 END,"
            "a.priority_score DESC,a.appointment_datetime", (doctor_id,))]
        proposals = [dict(r) for r in conn.execute(
            "SELECT sp.*,af.patient_id AS affected_patient_id,pa.display_name AS affected_patient_name,"
            "pi.display_name AS incoming_patient_name FROM scheduling_proposals sp "
            "JOIN appointments af ON af.appointment_id=sp.affected_appointment_id "
            "JOIN appointments inc ON inc.appointment_id=sp.incoming_appointment_id "
            "JOIN patients pa ON pa.patient_id=af.patient_id JOIN patients pi ON pi.patient_id=inc.patient_id "
            "WHERE af.doctor_id=? AND inc.doctor_id=? ORDER BY sp.proposal_id DESC", (doctor_id, doctor_id))]
        reminders = [dict(r) for r in conn.execute(
            "SELECT r.* FROM reminders r JOIN appointments a ON a.appointment_id=r.appointment_id "
            "WHERE a.doctor_id=? ORDER BY r.scheduled_at DESC,r.reminder_id DESC", (doctor_id,))]
        requests = [dict(r) for r in conn.execute(
            "SELECT ar.*,a.patient_id FROM appointment_requests ar JOIN appointments a ON a.appointment_id=ar.appointment_id "
            "WHERE a.doctor_id=? ORDER BY ar.request_id DESC", (doctor_id,))]
        tasks = [dict(r) for r in conn.execute(
            "SELECT t.* FROM staff_review_tasks t JOIN appointments a ON a.appointment_id=t.appointment_id "
            "WHERE a.doctor_id=? ORDER BY t.task_id DESC", (doctor_id,))]
        return {"doctor": doctor, "appointments": appointments, "proposals": proposals,
                "reminders": reminders, "requests": requests, "staff_review_tasks": tasks}


def resolve_staff_review_task(task_id, doctor_id, resolution, *, db_path=None, now=None):
    if not isinstance(resolution, str) or not resolution.strip() or len(resolution) > 4000:
        raise ValidationError("A written staff-review resolution is required.")
    try:
        with db.transaction(db_path) as conn:
            task = db.required_row(conn, "staff_review_tasks", task_id)
            appointment = db.required_row(conn, "appointments", task["appointment_id"])
            if not doctor_id or appointment["doctor_id"] != doctor_id:
                raise AuthorizationError("Only the assigned doctor can resolve this task.")
            if task["task_status"] == "resolved":
                return task
            conn.execute("UPDATE staff_review_tasks SET task_status='resolved',resolution=?,updated_at=? WHERE task_id=?",
                         (resolution.strip(), to_storage(utc_now(now)), task_id))
            db.log_action(conn, task["appointment_id"], "staff_review_resolved", resolution, f"doctor:{doctor_id}",
                          details={"task_id": task_id}, now=now)
            return db.required_row(conn, "staff_review_tasks", task_id)
    except Exception as exc:
        db.log_failure("operation_failed", f"Staff review update failed: {exc}", actor=f"doctor:{doctor_id}", db_path=db_path, now=now)
        raise
