
import sqlite3
import uuid
import bcrypt
from pathlib import Path

DB_PATH = Path(__file__).with_name("patients.db")


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn





def init_db():
    with connect() as conn:

        # 1. Patients table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS patients (
                patient_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash BLOB NOT NULL,
                dob TEXT NOT NULL,
                gender TEXT NOT NULL,
                weight REAL,
                bp_history TEXT DEFAULT '',
                heart_history TEXT DEFAULT '',
                conditions TEXT DEFAULT '',
                medications TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Add created_at to an older patients table if missing
        patient_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(patients)")
        }

        if "created_at" not in patient_columns:
            conn.execute("""
                ALTER TABLE patients
                ADD COLUMN created_at TEXT
            """)
            conn.execute("""
                UPDATE patients
                SET created_at = CURRENT_TIMESTAMP
                WHERE created_at IS NULL
            """)

        # 2. Symptom assessments table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS assessments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id TEXT NOT NULL,
                symptoms TEXT NOT NULL,
                duration TEXT NOT NULL,
                severity TEXT NOT NULL,
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (patient_id)
                    REFERENCES patients(patient_id)
            )
        """)

        # 3. Appointments table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS appointments (
                appointment_id TEXT PRIMARY KEY,
                patient_id TEXT NOT NULL,
                specialty TEXT NOT NULL,
                appt_date TEXT NOT NULL,
                appt_time TEXT NOT NULL,
                status TEXT DEFAULT 'Scheduled',
                priority_score REAL,
                FOREIGN KEY (patient_id)
                    REFERENCES patients(patient_id)
            )
        """)

        # Add priority_score if an older appointments table lacks it
        appointment_columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(appointments)"
            )
        }

        if "priority_score" not in appointment_columns:
            conn.execute("""
                ALTER TABLE appointments
                ADD COLUMN priority_score REAL
            """)




def register_patient(data):
    email = data["email"].strip().lower()
    password = data["password"]

    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")

    patient_id = "P-" + uuid.uuid4().hex[:12].upper()
    password_hash = bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt()
    )

    with connect() as conn:
        conn.execute("""
            INSERT INTO patients (
                patient_id, name, email, password_hash,
                dob, gender, weight, bp_history,
                heart_history, conditions, medications
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            patient_id, data["name"].strip(), email,
            password_hash, data["dob"], data["gender"],
            data["weight"], data["bp_history"],
            data["heart_history"], data["conditions"],
            data["medications"]
        ))

    return patient_id


def authenticate(email, password):
    with connect() as conn:
        patient = conn.execute("""
            SELECT patient_id, password_hash
            FROM patients WHERE email = ?
        """, (email.strip().lower(),)).fetchone()

    if patient and bcrypt.checkpw(
        password.encode("utf-8"), patient["password_hash"]
    ):
        return patient["patient_id"]

    return None


def get_patient(patient_id):
    with connect() as conn:
        return conn.execute("""
            SELECT patient_id, name, email, dob, gender, weight,
                   bp_history, heart_history, conditions, medications,
                   created_at
            FROM patients WHERE patient_id = ?
        """, (patient_id,)).fetchone()

def save_assessment(patient_id, symptoms, duration, severity, notes=""):
    with connect() as conn:
        conn.execute("""
            INSERT INTO assessments (
                patient_id,
                symptoms,
                duration,
                severity,
                notes
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            patient_id,
            symptoms,
            duration,
            severity,
            notes
        ))


def get_assessments(patient_id):
    with connect() as conn:
        return conn.execute("""
            SELECT id, patient_id, symptoms, duration,
                   severity, notes, created_at
            FROM assessments
            WHERE patient_id = ?
            ORDER BY created_at DESC
        """, (patient_id,)).fetchall()

def get_appointments(patient_id):
    with connect() as conn:
        return conn.execute("""
            SELECT *
            FROM appointments
            WHERE patient_id = ?
            ORDER BY appt_date, appt_time
        """, (patient_id,)).fetchall()




if __name__ == "__main__":
    init_db()
    print("Database initialized successfully.")
