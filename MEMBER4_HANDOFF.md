# Member 4 — essential backend handoff

**Send this bundle, not the full demo project.** It contains seven required backend/schema files plus this short handoff note. The archive excludes the app, dashboards, database file, mock engine, tests and virtual environment. The backend uses only Python 3.11+ standard libraries; no additional pip requirements are needed.

## Files

- `database.py`: one shared SQLite connection/schema/audit layer; initialization and optional synthetic seed helpers.
- `scheduler.py`: real availability, interval-safe bookings, proposals, consent and atomic schedule changes.
- `reminder_manager.py`: simulated 48h/24h reminders and idempotent processing.
- `integration.py`: public services for Members 1–3; accepts Member 3's actual engine.
- `time_utils.py`, `errors.py`: required internal dependencies.
- `schema.sql`: exact shared schema, indexes and overlap/immutable-consent triggers.

The ZIP is flat. Merge these files into the team's integrated project folder; retain the other members' dashboards, login and priority code. If their database module/schema already differs, agree a mapping/migration rather than blindly overwrite it. The initializer refuses incompatible recognized tables without replacing data.

## One shared database

Initialize a new compatible database once: `python database.py`. It defaults to `clinic.db` beside the module; no sample rows are automatically inserted. Do not send/copy separate database files to each dashboard. All integrated processes must access the same actual local file, optionally configured with one absolute `CLINIC_DB_PATH`. Identical filenames on different laptops are not a shared database; integrate/run on one laptop for this MVP. Avoid OneDrive/network-share synchronization for the live SQLite file.

Patients, doctors and doctor availability must be present in the agreed shared schema. Existing optional `seed_reference_data`/`seed_demo_data` helpers are for synthetic local testing only, not real team/clinical records. Stored timestamps are UTC; date/naive datetime inputs and display use Asia/Kolkata (UTC+05:30).

## Member 1: call `integration.py` from the existing patient UI

Use `get_available_slots`, `is_slot_available`, `book_appointment`, `submit_appointment_request`, `request_cancellation`, `request_rescheduling`, `cancel_appointment`, `respond_to_proposal`, and `get_patient_dashboard_data`.

```python
import integration as scheduling

# IDs must come from the team's authenticated session.
result = scheduling.book_appointment(
    patient_id, doctor_id, selected_start, 30, engine=member3_adapter)
view = scheduling.get_patient_dashboard_data(patient_id)
```

For an occupied preferred time, save an intake with `submit_appointment_request(...)`, then call `process_new_appointment(appointment_id, engine=member3_adapter)`. Intakes are NOT confirmed bookings. Check `result['outcome']` and `result['appointment']['appointment_status']`. Refresh queries after writes; do not cache appointment truth in session state.

## Member 2: call the same services from the existing doctor UI

Use `get_doctor_dashboard_data`, `approve_proposal(proposal_id, doctor_id)`, `reject_proposal(proposal_id, doctor_id, reason)`, `confirm_rescheduling(proposal_id, doctor_id)`, `review_patient_request(request_id, doctor_id, approve, reason)` and `resolve_staff_review_task`.

Approval alone never moves an appointment. The affected patient separately calls `respond_to_proposal(proposal_id, patient_id, agree=True)`. Finalization requires both responses and revalidates both slots/versions atomically. Check the returned `outcome`: `confirmed` or `conflict`. The original appointments remain intact on conflict; proposals do not reserve slots.

## Member 3: supply the actual adapter

```python
# Callable interface; map YOUR existing output, without re-scoring.
def member3_adapter(appointment: dict, patient: dict) -> dict:
    return actual_member3_evaluation(appointment, patient)

# result = scheduling.process_new_appointment(appointment_id, engine=member3_adapter)
```

Required returned keys: `priority_score` (finite number, 0–100), `risk_flags` (list of strings), `reason` (nonempty string), `recommended_action`. Normalized actions: `routine_booking`, `expedite`, `escalate_to_staff`, `emergency_review`. Optional `urgency`: routine/priority/urgent/emergency; optional `source`. Map different names/action labels in your adapter or `validate_priority_result`; never add Member 4 scoring.

If `engine=` is omitted, the adapter lazily looks for the team's `priority_engine.evaluate_priority(appointment, patient)`. No engine/mock file is supplied. Missing/failed engines produce a logged `engine_error` and staff-review task rather than an import crash or invented score. Pass the real callable explicitly to avoid accidentally using an old demo mock.

## Reminders and safety

Call `integration.process_due_reminders()` once on application open/full refresh. There is no worker and no actual SMS/email delivery. Late reminder windows, cancellations, completion and superseded schedules are recorded as skipped; processing is idempotent. Risk flags and urgent/staff/emergency actions route to human review, not automated patient displacement. Scores are not diagnoses. These internal services are not an authentication/security replacement.
