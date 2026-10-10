
# doctor_dashboard.py
# Healthcare Appointment Flow Optimizer
# Prototype only — uses synthetic demo data when the database is empty.

import sqlite3
from pathlib import Path
from datetime import date, datetime

import pandas as pd
import streamlit as st


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

DB_PATH = Path(__file__).resolve().parent / "healthcare.db"

st.set_page_config(
    page_title="Doctor Dashboard",
    page_icon="🩺",
    layout="wide",
)


# --------------------------------------------------
# DATABASE
# Replace these helpers with your team's database.py
# functions if a shared schema already exists.
# --------------------------------------------------

def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize_database():
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS patients (
                patient_id TEXT PRIMARY KEY,
                patient_name TEXT NOT NULL,
                age INTEGER,
                phone TEXT,
                medical_history TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS appointments (
                appointment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id TEXT NOT NULL,
                appointment_date TEXT NOT NULL,
                appointment_time TEXT NOT NULL,
                symptoms TEXT DEFAULT '',
                priority_score REAL DEFAULT 0,
                priority_level TEXT DEFAULT 'Routine',
                risk_flags TEXT DEFAULT '',
                priority_reason TEXT DEFAULT '',
                status TEXT DEFAULT 'Booked',
                FOREIGN KEY (patient_id)
                    REFERENCES patients(patient_id),
                UNIQUE (appointment_date, appointment_time)
            );

            CREATE TABLE IF NOT EXISTS medical_history (
                history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id TEXT NOT NULL,
                visit_date TEXT,
                notes TEXT,
                FOREIGN KEY (patient_id)
                    REFERENCES patients(patient_id)
            );

            CREATE TABLE IF NOT EXISTS schedule_proposals (
                proposal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                appointment_id INTEGER NOT NULL,
                old_date TEXT NOT NULL,
                old_time TEXT NOT NULL,
                proposed_date TEXT NOT NULL,
                proposed_time TEXT NOT NULL,
                reason TEXT DEFAULT '',
                status TEXT DEFAULT 'Pending',
                FOREIGN KEY (appointment_id)
                    REFERENCES appointments(appointment_id)
            );
        """)


def seed_demo_data():
    """Insert synthetic records only when the database is empty."""
    with get_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM appointments"
        ).fetchone()[0]

        if count:
            return

        today = date.today().isoformat()

        demo_patients = [
            ("P102", "Patient A", 55, "Demo contact",
             "Cardiac history recorded"),
            ("P105", "Patient B", 38, "Demo contact",
             "No cardiac history recorded"),
            ("P108", "Patient C", 29, "Demo contact",
             "Routine follow-up"),
        ]

        conn.executemany("""
            INSERT INTO patients
            (patient_id, patient_name, age, phone, medical_history)
            VALUES (?, ?, ?, ?, ?)
        """, demo_patients)

        demo_appointments = [
            ("P102", today, "09:00", "Chest discomfort",
             80, "High", "Cardiac history",
             "Demo score; clinician review required", "Booked"),
            ("P105", today, "10:00", "Persistent headache",
             45, "Medium", "",
             "Demo priority score", "Booked"),
            ("P108", today, "11:00", "Routine follow-up",
             20, "Routine", "",
             "Routine appointment", "Booked"),
        ]

        conn.executemany("""
            INSERT INTO appointments
            (patient_id, appointment_date, appointment_time,
             symptoms, priority_score, priority_level, risk_flags,
             priority_reason, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, demo_appointments)

        conn.execute("""
            INSERT INTO medical_history
            (patient_id, visit_date, notes)
            VALUES (?, ?, ?)
        """, (
            "P102", today,
            "Synthetic demo record: cardiac history recorded."
        ))


# --------------------------------------------------
# DATA ACCESS
# --------------------------------------------------

def get_appointments(selected_date, search_text="", status_filter="Active"):
    sql = """
        SELECT
            a.appointment_id,
            a.patient_id,
            p.patient_name,
            p.age,
            a.appointment_date,
            a.appointment_time,
            a.symptoms,
            a.priority_score,
            a.priority_level,
            a.risk_flags,
            a.priority_reason,
            a.status,
            p.medical_history
        FROM appointments a
        JOIN patients p ON p.patient_id = a.patient_id
        WHERE a.appointment_date = ?
    """
    params = [selected_date]

    if status_filter == "Active":
        sql += " AND a.status IN ('Booked', 'Confirmed')"
    elif status_filter == "Completed":
        sql += " AND a.status = 'Completed'"
    elif status_filter == "Cancelled":
        sql += " AND a.status = 'Cancelled'"

    if search_text:
        sql += """
            AND (p.patient_name LIKE ? OR p.patient_id LIKE ?)
        """
        params.extend([f"%{search_text}%", f"%{search_text}%"])

    sql += """
        ORDER BY a.priority_score DESC,
                 a.appointment_time ASC
    """

    with get_connection() as conn:
        return pd.read_sql_query(sql, conn, params=params)


def get_patient_history(patient_id):
    with get_connection() as conn:
        return pd.read_sql_query("""
            SELECT visit_date, notes
            FROM medical_history
            WHERE patient_id = ?
            ORDER BY visit_date DESC
        """, conn, params=[patient_id])


def get_proposals():
    with get_connection() as conn:
        return pd.read_sql_query("""
            SELECT
                s.proposal_id,
                s.appointment_id,
                p.patient_name,
                s.old_date,
                s.old_time,
                s.proposed_date,
                s.proposed_time,
                s.reason,
                s.status
            FROM schedule_proposals s
            JOIN appointments a
                ON a.appointment_id = s.appointment_id
            JOIN patients p ON p.patient_id = a.patient_id
            WHERE s.status = 'Pending'
            ORDER BY s.proposed_date, s.proposed_time
        """, conn)


# --------------------------------------------------
# DOCTOR ACTIONS
# --------------------------------------------------

def mark_appointment(appointment_id, new_status):
    with get_connection() as conn:
        conn.execute("""
            UPDATE appointments
            SET status = ?
            WHERE appointment_id = ?
        """, (new_status, appointment_id))


def review_proposal(proposal_id, approved):
    """Apply a schedule change only after doctor approval.

    This prototype updates the appointment atomically.
    A production workflow must also obtain the affected
    patient's agreement before confirming a move.
    """
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")

        proposal = conn.execute("""
            SELECT * FROM schedule_proposals
            WHERE proposal_id = ? AND status = 'Pending'
        """, (proposal_id,)).fetchone()

        if not proposal:
            raise ValueError("This proposal is no longer pending.")

        if approved:
            # Do not overwrite a slot that has since changed.
            current = conn.execute("""
                SELECT appointment_date, appointment_time
                FROM appointments
                WHERE appointment_id = ?
            """, (proposal["appointment_id"],)).fetchone()

            if not current:
                raise ValueError("Appointment not found.")

            if (
                current["appointment_date"] != proposal["old_date"]
                or current["appointment_time"] != proposal["old_time"]
            ):
                raise ValueError(
                    "The original appointment has changed. Refresh and review."
                )

            conflict = conn.execute("""
                SELECT appointment_id FROM appointments
                WHERE appointment_date = ?
                  AND appointment_time = ?
                  AND status IN ('Booked', 'Confirmed')
                  AND appointment_id != ?
            """, (
                proposal["proposed_date"],
                proposal["proposed_time"],
                proposal["appointment_id"],
            )).fetchone()

            if conflict:
                raise ValueError(
                    "The proposed slot is already occupied."
                )

            conn.execute("""
                UPDATE appointments
                SET appointment_date = ?, appointment_time = ?
                WHERE appointment_id = ?
            """, (
                proposal["proposed_date"],
                proposal["proposed_time"],
                proposal["appointment_id"],
            ))

        conn.execute("""
            UPDATE schedule_proposals
            SET status = ?
            WHERE proposal_id = ?
        """, (
            "Approved" if approved else "Rejected",
            proposal_id,
        ))


# --------------------------------------------------
# AUTHENTICATION — DEMO ONLY
# --------------------------------------------------

def doctor_login():
    st.title("🩺 Doctor Login")
    st.caption("Demonstration login — not production authentication.")

    with st.form("doctor_login"):
        username = st.text_input("Doctor username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login", type="primary")

    if submitted:
        # Replace with your team's actual authentication service.
        if username == "doctor" and password == "demo123":
            st.session_state["doctor_logged_in"] = True
            st.rerun()
        else:
            st.error("Invalid demo credentials.")


# --------------------------------------------------
# DASHBOARD
# --------------------------------------------------

def main():
    initialize_database()
    seed_demo_data()

    if not st.session_state.get("doctor_logged_in", False):
        doctor_login()
        return

    with st.sidebar:
        st.title("🩺 Doctor Portal")
        st.caption("Healthcare Appointment Flow Optimizer")
        page = st.radio(
            "Navigate",
            [
                "Overview",
                "Patient Profiles",
                "Schedule Proposals",
            ],
        )

        if st.button("Logout"):
            st.session_state["doctor_logged_in"] = False
            st.rerun()

    st.title("Doctor Dashboard")
    st.caption(f"Signed in · {date.today().strftime('%d %b %Y')}")

    if page == "Overview":
        render_overview()
    elif page == "Patient Profiles":
        render_patient_profiles()
    else:
        render_schedule_proposals()


def render_overview():
    selected_date = st.date_input("Appointment date", value=date.today())
    search_text = st.text_input("Search patient by name or ID")
    status_filter = st.selectbox(
        "Appointment status",
        ["Active", "Completed", "Cancelled", "All"],
    )

    df = get_appointments(
        selected_date.isoformat(),
        search_text,
        status_filter,
    )

    if df.empty:
        st.info("No appointments match these filters.")
        return

    today_count = len(df)
    high_count = int((df["priority_level"] == "High").sum())
    pending_count = len(get_proposals())

    c1, c2, c3 = st.columns(3)
    c1.metric("Appointments shown", today_count)
    c2.metric("High-priority labels", high_count)
    c3.metric("Pending schedule proposals", pending_count)

    st.subheader("Prioritized appointment queue")
    st.caption(
        "Ordered by the saved agent score. The score is not a "
        "diagnosis or a substitute for clinical triage."
    )

    display = df[[
        "appointment_id", "patient_id", "patient_name",
        "appointment_time", "symptoms", "priority_score",
        "priority_level", "risk_flags", "status",
    ]].copy()

    display.columns = [
        "Appointment ID", "Patient ID", "Patient",
        "Time", "Symptoms", "Priority Score",
        "Priority", "Review Flags", "Status",
    ]

    st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
    )

    # Selecting a patient opens their details below.
    ids = df["appointment_id"].tolist()
    selected_id = st.selectbox(
        "Open appointment details",
        ids,
        format_func=lambda x: (
            f"#{x} — "
            f"{df.loc[df['appointment_id'] == x, 'patient_name'].iloc[0]}"
        ),
    )

    record = df[df["appointment_id"] == selected_id].iloc[0]

    st.subheader(f"Patient: {record['patient_name']}")
    left, right = st.columns(2)

    with left:
        st.markdown("**Appointment details**")
        st.write("Patient ID:", record["patient_id"])
        st.write("Age:", record["age"])
        st.write("Date:", record["appointment_date"])
        st.write("Time:", record["appointment_time"])
        st.write("Symptoms:", record["symptoms"])

    with right:
        st.markdown("**Medical review information**")
        st.write("Recorded history:", record["medical_history"] or "Not recorded")
        st.write("Review flags:", record["risk_flags"] or "None recorded")
        st.write("Agent explanation:", record["priority_reason"] or "Not provided")

    history = get_patient_history(record["patient_id"])
    with st.expander("Previous visit history"):
        if history.empty:
            st.info("No previous visits recorded.")
        else:
            st.dataframe(history, use_container_width=True, hide_index=True)

    st.markdown("**Appointment actions**")
    a, b = st.columns(2)

    with a:
        if st.button(
            "Mark as completed",
            key=f"complete_{selected_id}",
        ):
            mark_appointment(selected_id, "Completed")
            st.success("Appointment marked as completed.")
            st.rerun()

    with b:
        if st.button(
            "Flag for staff follow-up",
            key=f"flag_{selected_id}",
        ):
            st.session_state["staff_followup"] = {
                "appointment_id": int(selected_id),
                "patient_id": str(record["patient_id"]),
            }
            st.success("Flagged for staff follow-up in this session.")

    if st.session_state.get("staff_followup"):
        item = st.session_state["staff_followup"]
        st.warning(
            f"Staff follow-up: appointment #{item['appointment_id']} "
            f"for patient {item['patient_id']}. "
            "Persist this action to the shared action log for team use."
        )


def render_patient_profiles():
    st.subheader("Patient Profiles")

    with get_connection() as conn:
        patients = pd.read_sql_query("""
            SELECT patient_id, patient_name, age, medical_history
            FROM patients
            ORDER BY patient_name
        """, conn)

    if patients.empty:
        st.info("No patient profiles found.")
        return

    patient_id = st.selectbox(
        "Select patient",
        patients["patient_id"].tolist(),
        format_func=lambda x: (
            patients.loc[
                patients["patient_id"] == x, "patient_name"
            ].iloc[0] + f" ({x})"
        ),
    )

    patient = patients[patients["patient_id"] == patient_id].iloc[0]
    st.subheader(patient["patient_name"])
    st.write("Patient ID:", patient["patient_id"])
    st.write("Age:", patient["age"])
    st.write("Recorded medical history:", patient["medical_history"] or "Not recorded")

    appointments = get_appointments_for_patient(patient_id)
    st.markdown("**Appointments**")
    if appointments.empty:
        st.info("No appointments found.")
    else:
        st.dataframe(appointments, use_container_width=True, hide_index=True)

    st.markdown("**Previous visits**")
    history = get_patient_history(patient_id)
    if history.empty:
        st.info("No previous visits recorded.")
    else:
        st.dataframe(history, use_container_width=True, hide_index=True)


def get_appointments_for_patient(patient_id):
    with get_connection() as conn:
        return pd.read_sql_query("""
            SELECT appointment_id, appointment_date, appointment_time,
                   symptoms, priority_score, priority_level, status
            FROM appointments
            WHERE patient_id = ?
            ORDER BY appointment_date DESC, appointment_time DESC
        """, conn, params=[patient_id])


def render_schedule_proposals():
    st.subheader("Schedule Change Requests")
    st.caption(
        "Review proposals carefully. Do not move a patient without "
        "confirming the alternative with them."
    )

    proposals = get_proposals()

    if proposals.empty:
        st.success("There are no pending schedule proposals.")
        return

    for _, row in proposals.iterrows():
        with st.container(border=True):
            st.markdown(
                f"**{row['patient_name']}** · "
                f"Appointment #{row['appointment_id']}"
            )
            st.write(
                f"Current: {row['old_date']} at {row['old_time']}"
            )
            st.write(
                f"Proposed: {row['proposed_date']} "
                f"at {row['proposed_time']}"
            )
            st.write("Reason:", row["reason"] or "Not provided")

            st.caption(
                "Confirm patient agreement and slot availability "
                "before approving."
            )

            col1, col2 = st.columns(2)

            with col1:
                if st.button(
                    "Approve proposal",
                    key=f"approve_{row['proposal_id']}",
                ):
                    try:
                        review_proposal(int(row["proposal_id"]), True)
                        st.success("Proposal approved.")
                        st.rerun()
                    except (ValueError, sqlite3.IntegrityError) as exc:
                        st.error(str(exc))

            with col2:
                if st.button(
                    "Reject proposal",
                    key=f"reject_{row['proposal_id']}",
                ):
                    try:
                        review_proposal(int(row["proposal_id"]), False)
                        st.info("Proposal rejected.")
                        st.rerun()
                    except (ValueError, sqlite3.IntegrityError) as exc:
                        st.error(str(exc))


if __name__ == "__main__":
    main()
