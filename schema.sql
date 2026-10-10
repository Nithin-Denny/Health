-- MVP v1. UTC instants have a fixed sortable YYYY-MM-DDTHH:MM:SSZ representation.
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS doctors (
    doctor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    default_duration_minutes INTEGER NOT NULL DEFAULT 30
        CHECK (default_duration_minutes BETWEEN 15 AND 180 AND default_duration_minutes % 15 = 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS doctor_availability (
    doctor_id TEXT NOT NULL REFERENCES doctors(doctor_id),
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    slot_interval_minutes INTEGER NOT NULL DEFAULT 30 CHECK (slot_interval_minutes > 0),
    PRIMARY KEY (doctor_id, weekday, start_time),
    CHECK (start_time < end_time)
);

CREATE TABLE IF NOT EXISTS patients (
    patient_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    questionnaire_json TEXT NOT NULL DEFAULT '{}',
    mock_scenario TEXT NOT NULL DEFAULT 'routine',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS appointments (
    appointment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    doctor_id TEXT NOT NULL REFERENCES doctors(doctor_id),
    appointment_datetime TEXT NOT NULL
        CHECK (length(appointment_datetime) = 20 AND appointment_datetime LIKE '%Z'
               AND strftime('%Y-%m-%dT%H:%M:%SZ', appointment_datetime) IS NOT NULL
               AND strftime('%Y-%m-%dT%H:%M:%SZ', appointment_datetime) = appointment_datetime),
    requested_datetime TEXT NOT NULL,
    latest_acceptable_datetime TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL DEFAULT 30
        CHECK (duration_minutes BETWEEN 15 AND 180 AND duration_minutes % 15 = 0),
    appointment_status TEXT NOT NULL DEFAULT 'requested'
        CHECK (appointment_status IN ('requested', 'booked', 'cancelled', 'completed')),
    confirmed INTEGER NOT NULL DEFAULT 0 CHECK (confirmed IN (0, 1)),
    priority_score REAL CHECK (priority_score BETWEEN 0 AND 100),
    risk_flags TEXT NOT NULL DEFAULT '[]',
    agent_reason TEXT,
    recommended_action TEXT,
    urgency TEXT NOT NULL DEFAULT 'unknown'
        CHECK (urgency IN ('unknown', 'routine', 'priority', 'urgent', 'emergency')),
    evaluation_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (evaluation_status IN ('pending', 'ready', 'error')),
    priority_evaluated_at TEXT,
    engine_source TEXT,
    engine_error TEXT,
    protected INTEGER NOT NULL DEFAULT 0 CHECK (protected IN (0, 1)),
    movable INTEGER NOT NULL DEFAULT 1 CHECK (movable IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 0,
    demo_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (latest_acceptable_datetime >= requested_datetime),
    CHECK ((appointment_status = 'booked' AND confirmed = 1)
        OR (appointment_status != 'booked' AND confirmed = 0))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_doctor_start
    ON appointments(doctor_id, appointment_datetime)
    WHERE appointment_status = 'booked';
CREATE INDEX IF NOT EXISTS ix_doctor_queue
    ON appointments(doctor_id, appointment_status, priority_score DESC, appointment_datetime);
CREATE INDEX IF NOT EXISTS ix_patient_appointments
    ON appointments(patient_id, appointment_datetime);

-- Unique start alone does NOT protect durations. These triggers reject ALL overlaps,
-- even for clients that bypass the Python layer. Adjacent intervals are allowed.
CREATE TRIGGER IF NOT EXISTS prevent_overlap_insert
BEFORE INSERT ON appointments
WHEN NEW.appointment_status = 'booked'
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM appointments a
        WHERE a.doctor_id = NEW.doctor_id AND a.appointment_status = 'booked'
          AND a.appointment_datetime < strftime('%Y-%m-%dT%H:%M:%SZ',
              NEW.appointment_datetime, '+' || NEW.duration_minutes || ' minutes')
          AND strftime('%Y-%m-%dT%H:%M:%SZ', a.appointment_datetime,
              '+' || a.duration_minutes || ' minutes') > NEW.appointment_datetime
    ) THEN RAISE(ABORT, 'active doctor appointment overlap') END;
END;

CREATE TRIGGER IF NOT EXISTS prevent_overlap_update
BEFORE UPDATE OF doctor_id, appointment_datetime, duration_minutes, appointment_status ON appointments
WHEN NEW.appointment_status = 'booked'
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM appointments a
        WHERE a.appointment_id != NEW.appointment_id
          AND a.doctor_id = NEW.doctor_id AND a.appointment_status = 'booked'
          AND a.appointment_datetime < strftime('%Y-%m-%dT%H:%M:%SZ',
              NEW.appointment_datetime, '+' || NEW.duration_minutes || ' minutes')
          AND strftime('%Y-%m-%dT%H:%M:%SZ', a.appointment_datetime,
              '+' || a.duration_minutes || ' minutes') > NEW.appointment_datetime
    ) THEN RAISE(ABORT, 'active doctor appointment overlap') END;
END;

CREATE TABLE IF NOT EXISTS scheduling_proposals (
    proposal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    incoming_appointment_id INTEGER NOT NULL REFERENCES appointments(appointment_id),
    affected_appointment_id INTEGER NOT NULL REFERENCES appointments(appointment_id),
    affected_original_datetime TEXT NOT NULL,
    proposed_datetime TEXT NOT NULL,
    incoming_target_datetime TEXT NOT NULL,
    incoming_version INTEGER NOT NULL,
    affected_version INTEGER NOT NULL,
    reason TEXT NOT NULL,
    proposal_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (proposal_status IN ('pending', 'doctor_approved', 'patient_agreed', 'ready',
                'confirmed', 'rejected', 'patient_declined', 'conflict', 'invalidated')),
    doctor_response TEXT NOT NULL DEFAULT 'pending'
        CHECK (doctor_response IN ('pending', 'approved', 'rejected')),
    patient_response TEXT NOT NULL DEFAULT 'pending'
        CHECK (patient_response IN ('pending', 'agreed', 'declined')),
    response_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    confirmed_at TEXT,
    CHECK (incoming_appointment_id != affected_appointment_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_proposal_incoming
    ON scheduling_proposals(incoming_appointment_id)
    WHERE proposal_status IN ('pending', 'doctor_approved', 'patient_agreed', 'ready');
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_proposal_affected
    ON scheduling_proposals(affected_appointment_id)
    WHERE proposal_status IN ('pending', 'doctor_approved', 'patient_agreed', 'ready');
CREATE INDEX IF NOT EXISTS ix_proposal_status ON scheduling_proposals(proposal_status);

-- Consent refers to specific immutable terms, even if a team client uses raw SQL.
CREATE TRIGGER IF NOT EXISTS freeze_proposal_terms
BEFORE UPDATE OF incoming_appointment_id, affected_appointment_id, affected_original_datetime,
    proposed_datetime, incoming_target_datetime, incoming_version, affected_version, reason
ON scheduling_proposals
BEGIN
    SELECT RAISE(ABORT, 'proposal terms are immutable; create a new proposal for fresh consent');
END;

CREATE TABLE IF NOT EXISTS reminders (
    reminder_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER NOT NULL REFERENCES appointments(appointment_id),
    appointment_datetime TEXT NOT NULL,
    reminder_type TEXT NOT NULL CHECK (reminder_type IN ('48h', '24h')),
    scheduled_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (status IN ('scheduled', 'simulated_sent', 'skipped', 'failed')),
    message TEXT NOT NULL,
    processed_at TEXT,
    status_reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (appointment_id, appointment_datetime, reminder_type)
);
CREATE INDEX IF NOT EXISTS ix_due_reminders ON reminders(status, scheduled_at);
CREATE INDEX IF NOT EXISTS ix_appointment_reminders ON reminders(appointment_id);

CREATE TABLE IF NOT EXISTS appointment_requests (
    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER NOT NULL REFERENCES appointments(appointment_id),
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    request_type TEXT NOT NULL CHECK (request_type IN ('cancellation', 'reschedule')),
    requested_datetime TEXT,
    appointment_version INTEGER NOT NULL,
    patient_agreed INTEGER NOT NULL DEFAULT 1 CHECK (patient_agreed IN (0, 1)),
    reason TEXT NOT NULL,
    request_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (request_status IN ('pending', 'approved', 'rejected', 'conflict', 'invalidated')),
    doctor_response TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (request_type != 'reschedule' OR requested_datetime IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_patient_request
    ON appointment_requests(appointment_id) WHERE request_status = 'pending';

CREATE TABLE IF NOT EXISTS staff_review_tasks (
    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER NOT NULL REFERENCES appointments(appointment_id),
    task_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    task_status TEXT NOT NULL DEFAULT 'open' CHECK (task_status IN ('open', 'resolved')),
    resolution TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_staff_task
    ON staff_review_tasks(appointment_id, task_type) WHERE task_status = 'open';

CREATE TABLE IF NOT EXISTS action_logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER REFERENCES appointments(appointment_id),
    proposal_id INTEGER REFERENCES scheduling_proposals(proposal_id),
    action_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_action_appointment ON action_logs(appointment_id, created_at);
