

import sqlite3
from datetime import date

import streamlit as st
import database as db


# ---------------- PAGE CONFIGURATION ----------------

st.set_page_config(
    page_title="Healthcare Appointment Optimizer",
    page_icon="🏥",
    layout="wide"
)

db.init_db()

if "patient_id" not in st.session_state:
    st.session_state.patient_id = None

st.title("🏥 Healthcare Appointment Optimizer")
st.caption("Patient Portal")


# ---------------- LOGIN / REGISTRATION ----------------

if not st.session_state.patient_id:

    register_tab, login_tab = st.tabs(
        ["Create Account", "Login"]
    )

    # ---------------- REGISTRATION ----------------

    with register_tab:
        st.subheader("Register as a patient")

        with st.form("registration_form"):
            name = st.text_input("Full name")
            email = st.text_input("Email address")

            password = st.text_input(
                "Password (minimum 8 characters)",
                type="password"
            )

            confirm_password = st.text_input(
                "Confirm password",
                type="password"
            )

            dob = st.date_input(
                "Date of birth",
                value=date(2000, 1, 1),
                max_value=date.today()
            )

            gender = st.selectbox(
                "Gender",
                ["Prefer not to say", "Female", "Male", "Other"]
            )

            weight = st.number_input(
                "Weight (kg)",
                min_value=0.0,
                max_value=500.0,
                value=0.0,
                step=0.5
            )

            st.markdown("### Medical History")

            bp_history = st.text_input(
                "Blood pressure history",
                placeholder="e.g. Normal, hypertension, or unknown"
            )

            heart_history = st.text_input(
                "Heart history",
                placeholder="Enter relevant history, or None"
            )

            conditions = st.text_area(
                "Existing medical conditions"
            )

            medications = st.text_area(
                "Current medications"
            )

            submitted = st.form_submit_button(
                "Create Patient Account",
                use_container_width=True
            )

        if submitted:

            if not name.strip() or not email.strip():
                st.error("Please enter your name and email.")

            elif password != confirm_password:
                st.error("The passwords do not match.")

            elif len(password) < 8:
                st.error("Password must be at least 8 characters.")

            elif dob >= date.today():
                st.error("Please enter a valid date of birth.")

            else:
                patient_data = {
                    "name": name,
                    "email": email,
                    "password": password,
                    "dob": dob.isoformat(),
                    "gender": gender,
                    "weight": weight if weight > 0 else None,
                    "bp_history": bp_history,
                    "heart_history": heart_history,
                    "conditions": conditions,
                    "medications": medications
                }

                try:
                    patient_id = db.register_patient(patient_data)

                    st.success("Account created successfully!")

                    st.write("Your unique Patient ID:")
                    st.code(patient_id)

                    st.info(
                        "Please save your Patient ID. "
                        "You can now log in using your email and password."
                    )

                except sqlite3.IntegrityError:
                    st.error(
                        "This email is already registered. "
                        "Please log in instead."
                    )

                except ValueError as error:
                    st.error(str(error))

                except sqlite3.Error:
                    st.error(
                        "The database could not save your account. "
                        "Please try again."
                    )

    # ---------------- LOGIN ----------------

    with login_tab:
        st.subheader("Login to your account")

        with st.form("login_form"):
            login_email = st.text_input(
                "Email address",
                key="login_email"
            )

            login_password = st.text_input(
                "Password",
                type="password",
                key="login_password"
            )

            login_submitted = st.form_submit_button(
                "Login",
                use_container_width=True
            )

        if login_submitted:

            try:
                patient_id = db.authenticate(
                    login_email,
                    login_password
                )

                if patient_id:
                    st.session_state.patient_id = patient_id
                    st.rerun()

                else:
                    st.error("Incorrect email or password.")

            except sqlite3.Error:
                st.error(
                    "Could not access the database. Please try again."
                )


# ---------------- LOGGED-IN PATIENT DASHBOARD ----------------

else:

    patient_id = st.session_state.patient_id
    patient = db.get_patient(patient_id)

    if patient is None:
        st.error("Patient record not found. Please log in again.")
        st.session_state.patient_id = None
        st.rerun()

    # ---------------- LEFT SIDEBAR ----------------

    with st.sidebar:

        st.header("👤 Patient Profile")

        st.write("**Patient ID**")
        st.code(patient["patient_id"])

        st.write("**Name:**", patient["name"])
        st.write("**Email:**", patient["email"])
        st.write("**Date of birth:**", patient["dob"])
        st.write("**Gender:**", patient["gender"])

        st.write(
            "**Weight:**",
            f"{patient['weight']} kg"
            if patient["weight"] is not None
            else "Not provided"
        )

        st.divider()

        st.subheader("🩺 Medical History")

        st.write(
            "**Blood pressure:**",
            patient["bp_history"] or "Not provided"
        )

        st.write(
            "**Heart history:**",
            patient["heart_history"] or "Not provided"
        )

        st.write(
            "**Existing conditions:**",
            patient["conditions"] or "Not provided"
        )

        st.write(
            "**Medications:**",
            patient["medications"] or "Not provided"
        )

        st.divider()

        if st.button("Logout", use_container_width=True):
            st.session_state.patient_id = None
            st.rerun()

    # ---------------- MAIN DASHBOARD ----------------

    st.subheader(f"Welcome, {patient['name']}!")

    symptoms_tab, appointments_tab = st.tabs(
        ["Current Symptoms", "My Appointments"]
    )

    # ==================================================
    # CURRENT SYMPTOMS
    # ==================================================

    with symptoms_tab:

        st.subheader("📝 Current Symptoms")

        st.write(
            "Describe your current symptoms. Your information "
            "will be saved for the scheduling agent to review."
        )

        with st.form("symptoms_form"):

            symptoms = st.selectbox(
    "Describe your symptoms",
    [
        "Select a symptom",
        "Fever",
        "Headache",
        "Cough",
        "Sore throat",
        "Difficulty breathing",
        "Chest pain",
        "Abdominal pain",
        "Nausea or vomiting",
        "Dizziness",
        "Fatigue",
        "Body pain",
        "Other"
    ]
)

            duration = st.selectbox(
                "How long have you had these symptoms?",
                [
                    "Less than 24 hours",
                    "1–3 days",
                    "4–7 days",
                    "More than a week"
                ]
            )

            severity = st.selectbox(
             "Symptom severity",
            ["Select severity", "Mild", "Moderate", "Severe"]
)

            notes = st.text_area(
                "Additional information (optional)"
            )

            submitted_symptoms = st.form_submit_button(
                "Submit Symptoms",
                use_container_width=True
            )

        if submitted_symptoms:

            if not symptoms.strip():
                st.error("Please describe your symptoms.")

            else:
                try:
                    db.save_assessment(
                        patient_id,
                        symptoms.strip(),
                        duration,
                        severity,
                        notes.strip()
                    )

                    st.success(
                        "Your symptoms have been submitted successfully. "
                        "The scheduling agent can now review your case."
                    )

                except (sqlite3.Error, AttributeError):
                    st.error(
                        "Could not save your symptoms. "
                        "Please check the assessment functions in database.py."
                    )

        st.divider()

        st.subheader("📋 Previous Assessments")

        try:
            assessments = db.get_assessments(patient_id)

            if assessments:

                for assessment in assessments:

                    with st.container(border=True):

                        st.write(
                            f"**{assessment['created_at']}** "
                            f"— {assessment['severity']}"
                        )

                        st.write(
                            "**Symptoms:**",
                            assessment["symptoms"]
                        )

                        st.write(
                            "**Duration:**",
                            assessment["duration"]
                        )

                        st.write(
                            "**Notes:**",
                            assessment["notes"] or "None"
                        )

            else:
                st.info(
                    "You have not submitted any symptom assessments yet."
                )

        except (sqlite3.Error, AttributeError):
            st.warning(
                "Previous assessments could not be loaded."
            )

    # ==================================================
    # MY APPOINTMENTS
    # ==================================================

    with appointments_tab:

        st.subheader("📅 My Appointments")

        try:
            appointments = db.get_appointments(patient_id)

            scheduled_appointments = [
                appointment
                for appointment in appointments
                if appointment["status"] == "Scheduled"
            ]

            if scheduled_appointments:

                st.success("Your appointment has been scheduled!")

                for appointment in scheduled_appointments:

                    with st.container(border=True):

                        st.write(
                            "**Appointment ID:**",
                            appointment["appointment_id"]
                        )

                        st.write(
                            "**Specialty:**",
                            appointment["specialty"]
                        )

                        st.write(
                            "**Appointment date:**",
                            appointment["appt_date"]
                        )

                        st.write(
                            "**Appointment time:**",
                            appointment["appt_time"]
                        )

                        st.write(
                            "**Status:**",
                            appointment["status"]
                        )

            else:
                st.info(
                    "Your appointment is pending scheduling. "
                    "We will notify you once your appointment has "
                    "been scheduled."
                )

        except (sqlite3.Error, AttributeError):
            st.info(
                "Your appointment is pending scheduling. "
                "Appointment details will appear here once assigned."
            )

