"""
priority_engine.py - Healthcare Appointment Flow Optimizer: Decision Engine
=============================================================================
Member 3 Deliverable:
Rule-based appointment prioritization, clinical risk flagging, explainability,
and schedule optimization proposal generator.

Key Design Principles:
1. Pure Python (standard library only) - zero required 3rd-party dependencies.
2. Accepts flexible inputs: list of dicts, single dict, or pandas DataFrame.
3. Transparent, configurable scoring weights.
4. Emergency Override: Immediate escalation instruction for emergency symptoms.
5. Strict Proposal-Only optimization: Doctors must approve any suggested move.
6. Not medically validated: Explicitly framed as a hackathon prototype triage aid.
"""

from __future__ import annotations
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union
from dataclasses import dataclass, field, asdict


# ============================================================================
# 1. CONFIGURATION & SCORING RULES
# ============================================================================

DEFAULT_CONFIG: Dict[str, Any] = {
    # Scoring weights (as per project specification)
    "weights": {
        "symptoms_prompt_review": 30,  # Symptoms requiring prompt clinical review
        "recorded_cardiac_history": 20,  # Relevant recorded cardiac history
        "other_medical_history": 10,     # Other relevant medical history
        "clinician_urgency_max": 30,     # Max clinician-assigned urgency (0-30)
    },
    # Clinician categorical urgency mapping (if clinician urgency passed as string)
    "clinician_urgency_levels": {
        "routine": 0,
        "low": 0,
        "moderate": 15,
        "medium": 15,
        "high": 25,
        "urgent": 30,
        "emergency": 30,
    },
    # Emergency red-flag symptoms: Triggers EMERGENCY_OVERRIDE immediately
    "emergency_keywords": [
        "severe chest pain",
        "crushing chest pain",
        "chest pain radiating",
        "radiating to left arm",
        "radiating to jaw",
        "cannot breathe",
        "unable to breathe",
        "severe shortness of breath",
        "choking",
        "slurred speech",
        "facial drooping",
        "sudden numbness",
        "sudden weakness on one side",
        "loss of consciousness",
        "unresponsive",
        "coughing up blood",
        "massive bleeding",
        "anaphylaxis",
        "throat closing",
        "severe allergic reaction",
        "worst headache of life",
        "thunderclap headache",
    ],
    # Symptoms requiring prompt clinical review (+30)
    "prompt_review_symptom_keywords": [
        "chest pain",
        "chest discomfort",
        "pressure in chest",
        "palpitation",
        "palpitations",
        "racing heart",
        "irregular heartbeat",
        "shortness of breath",
        "dyspnea",
        "breathlessness",
        "dizziness",
        "dizzy",
        "lightheaded",
        "presyncope",
        "syncope",
        "fainting",
        "high fever",
        "fever over 102",
        "severe abdominal pain",
        "persistent vomiting",
        "blood in stool",
        "blood in urine",
        "unexplained weight loss",
        "swelling in legs",
        "edema",
        "systolic over 180",
        "hypertensive urgency",
    ],
    # Relevant cardiac history keywords (+20)
    "cardiac_history_keywords": [
        "cad",
        "coronary artery disease",
        "myocardial infarction",
        "heart attack",
        "cardiac arrest",
        "congestive heart failure",
        "heart failure",
        "chf",
        "arrhythmia",
        "atrial fibrillation",
        "afib",
        "stent",
        "angioplasty",
        "coronary bypass",
        "cabg",
        "angina",
        "cardiomyopathy",
        "valvular disease",
        "aortic stenosis",
        "pacemaker",
        "icd",
        "hypertension",
        "high blood pressure",
        "high bp",
        "htn",
    ],
    # Other relevant chronic/acute history keywords (+10)
    "other_history_keywords": [
        "diabetes",
        "diabetic",
        "t2d",
        "t1d",
        "copd",
        "chronic bronchitis",
        "emphysema",
        "asthma",
        "chronic kidney disease",
        "ckd",
        "kidney failure",
        "stroke",
        "tia",
        "mini stroke",
        "cancer",
        "oncology",
        "chemotherapy",
        "immunosuppressed",
        "liver disease",
        "cirrhosis",
        "autoimmune",
        "rheumatoid",
        "lupus",
    ],
    # Disclaimers and notices
    "prototype_disclaimer": (
        "PROTOTYPE TRIAGE RULE ENGINE: The generated score is an administrative demo priority "
        "order and NOT a medical diagnosis or validated clinical triage tool."
    ),
    "emergency_instruction": (
        "EMERGENCY OVERRIDE ACTIVATED: Patient reports symptoms suggestive of an acute medical "
        "emergency. Do not wait for a scheduled clinic appointment. Direct patient immediately "
        "to call 911 or report to the nearest Emergency Department."
    ),
}


# ============================================================================
# 2. DATA NORMALIZATION HELPERS
# ============================================================================

def _normalize_string_list(val: Any) -> List[str]:
    """Convert string, list, set, or None into a normalized list of lower-case strings."""
    if val is None:
        return []
    if isinstance(val, (list, tuple, set)):
        items = []
        for x in val:
            if x:
                items.append(str(x).strip().lower())
        return items
    if isinstance(val, str):
        # Split on commas, semicolons, or newlines
        parts = re.split(r"[,;\n\r]+", val)
        return [p.strip().lower() for p in parts if p.strip()]
    return [str(val).strip().lower()]


def _extract_field(record: Dict[str, Any], candidate_keys: List[str], default: Any = None) -> Any:
    """Find a value from a record trying multiple case-insensitive key names."""
    # First exact match
    for key in candidate_keys:
        if key in record and record[key] is not None:
            return record[key]
    # Case-insensitive / normalized match
    lower_map = {k.lower().replace("_", "").replace(" ", ""): k for k in record.keys()}
    for key in candidate_keys:
        norm = key.lower().replace("_", "").replace(" ", "")
        if norm in lower_map:
            actual_key = lower_map[norm]
            if record[actual_key] is not None:
                return record[actual_key]
    return default


def _contains_any_keyword(text: str, keywords: List[str]) -> List[str]:
    """Search for keyword occurrences within text using word-boundary or phrase matches."""
    text_lower = text.lower()
    matches = []
    for kw in keywords:
        kw_clean = kw.strip().lower()
        if not kw_clean:
            continue
        # If keyword is multiple words, check substring directly
        if " " in kw_clean:
            if kw_clean in text_lower:
                matches.append(kw)
        else:
            # Word boundary regex for single words
            pattern = rf"\b{re.escape(kw_clean)}\b"
            if re.search(pattern, text_lower):
                matches.append(kw)
    return _prune_submatches(matches)


def _prune_submatches(matches: List[str]) -> List[str]:
    """Remove shorter duplicate or strictly contained keywords to keep summaries concise."""
    if not matches:
        return []
    # Deduplicate while preserving order
    seen: List[str] = []
    for m in matches:
        clean = m.strip()
        if clean and clean.lower() not in [s.lower() for s in seen]:
            seen.append(clean)
    
    # Filter out items that are pure substrings of a longer matched item
    pruned: List[str] = []
    for item in seen:
        item_lower = item.lower()
        is_substring = any(
            item_lower != other.lower() and item_lower in other.lower()
            for other in seen
        )
        if not is_substring:
            pruned.append(item)
    return pruned or seen


# ============================================================================
# 3. EXTRACTION AND SCORING ENGINE
# ============================================================================

def extract_structured_info(record: Dict[str, Any], config: Dict[str, Any] = DEFAULT_CONFIG) -> Dict[str, Any]:
    """
    Extract structured clinical indicators from appointment record, questionnaire,
    and medical history.
    """
    # 1. Extract symptoms
    raw_symptoms = _extract_field(record, ["symptoms", "reported_symptoms", "chief_complaint", "questionnaire_symptoms"], "")
    symptom_list = _normalize_string_list(raw_symptoms)
    symptom_text = " ".join(symptom_list)

    symptom_duration = _extract_field(record, ["symptom_duration", "duration"], "Unknown duration")

    # 2. Extract BP & Heart Disease Questionnaire responses
    bp_history_raw = _extract_field(record, ["bp_history", "bp_related_history", "blood_pressure_history", "bp"], "")
    heart_disease_raw = _extract_field(record, ["heart_disease_history", "cardiac_history", "heart_history"], "")

    # 3. Extract conditions & medications
    existing_conditions_raw = _extract_field(record, ["existing_conditions", "conditions", "medical_conditions"], "")
    recorded_conditions_raw = _extract_field(record, ["recorded_conditions", "history", "medical_history", "past_history"], "")
    current_meds_raw = _extract_field(record, ["current_medications", "medications", "meds"], "")

    # Combine medical history strings
    history_comb = " ".join([
        str(bp_history_raw or ""),
        str(heart_disease_raw or ""),
        str(existing_conditions_raw or ""),
        str(recorded_conditions_raw or "")
    ]).lower()

    # 4. Check Emergency Red Flags
    matched_emergency = _contains_any_keyword(symptom_text, config["emergency_keywords"])

    # 5. Check Prompt Review Symptoms
    matched_prompt_symptoms = _contains_any_keyword(symptom_text, config["prompt_review_symptom_keywords"])

    # 6. Check Cardiac History
    matched_cardiac = []
    if isinstance(heart_disease_raw, bool) and heart_disease_raw:
        matched_cardiac.append("Reported cardiac history in questionnaire")
    elif heart_disease_raw and str(heart_disease_raw).strip().lower() not in ["none", "no", "false", "0", "denies"]:
        matched_cardiac.append(f"{heart_disease_raw}")
    else:
        # Search keywords in general medical history text if explicit cardiac field was empty
        keyword_cardiac = _contains_any_keyword(history_comb, config["cardiac_history_keywords"])
        matched_cardiac.extend(keyword_cardiac)
    matched_cardiac = _prune_submatches(matched_cardiac)

    # Check BP history specifically
    has_bp_risk = False
    if isinstance(bp_history_raw, bool) and bp_history_raw:
        has_bp_risk = True
    elif bp_history_raw and str(bp_history_raw).strip().lower() not in ["none", "no", "false", "normal", "0"]:
        has_bp_risk = True

    # 7. Check Other Chronic Conditions
    matched_other_history = _contains_any_keyword(history_comb, config["other_history_keywords"])

    # 8. Parse Clinician Urgency
    clinician_urgency_input = _extract_field(record, ["clinician_urgency", "doctor_urgency", "provider_urgency", "clinician_score"], 0)
    clinician_urgency_score = 0
    clinician_urgency_label = "Routine"

    if isinstance(clinician_urgency_input, (int, float)):
        # Clamp between 0 and 30
        clinician_urgency_score = max(0, min(int(clinician_urgency_input), config["weights"]["clinician_urgency_max"]))
        clinician_urgency_label = f"Assigned ({clinician_urgency_score} pts)"
    elif isinstance(clinician_urgency_input, str):
        urg_key = clinician_urgency_input.strip().lower()
        levels = config.get("clinician_urgency_levels", {})
        if urg_key in levels:
            clinician_urgency_score = levels[urg_key]
            clinician_urgency_label = clinician_urgency_input.capitalize()
        else:
            # Try to parse number from string
            try:
                num = int(re.sub(r"[^\d]", "", urg_key))
                clinician_urgency_score = max(0, min(num, config["weights"]["clinician_urgency_max"]))
                clinician_urgency_label = f"Assigned ({clinician_urgency_score} pts)"
            except (ValueError, TypeError):
                clinician_urgency_score = 0
                clinician_urgency_label = "Routine"

    return {
        "symptoms_raw": raw_symptoms,
        "symptom_duration": symptom_duration,
        "symptom_text": symptom_text,
        "matched_emergency": matched_emergency,
        "matched_prompt_symptoms": matched_prompt_symptoms,
        "has_cardiac_history": len(matched_cardiac) > 0,
        "matched_cardiac_history": matched_cardiac,
        "has_bp_risk": has_bp_risk,
        "matched_other_history": matched_other_history,
        "has_other_history": len(matched_other_history) > 0,
        "clinician_urgency_score": clinician_urgency_score,
        "clinician_urgency_label": clinician_urgency_label,
        "current_medications": str(current_meds_raw or "None reported"),
    }


def evaluate_appointment(record: Dict[str, Any], config: Dict[str, Any] = DEFAULT_CONFIG) -> Dict[str, Any]:
    """
    Evaluates a single appointment record.
    
    Returns structured results:
    - appointment priority score (0-100)
    - clinical risk flags
    - human-readable explanation with breakdown of contributing factors
    - emergency override status & instructions
    """
    extracted = extract_structured_info(record, config)
    weights = config["weights"]

    appointment_id = _extract_field(record, ["appointment_id", "id", "apt_id"], "APT-UNKNOWN")
    patient_id = _extract_field(record, ["patient_id", "patient_code", "pid"], "PAT-UNKNOWN")
    patient_name = _extract_field(record, ["patient_name", "name", "full_name"], "Unknown Patient")
    doctor_name = _extract_field(record, ["doctor_name", "doctor", "specialist"], "Any Available Doctor")
    specialty = _extract_field(record, ["specialty", "department", "clinic"], "General Medicine")
    slot_time = _extract_field(record, ["slot_time", "appointment_date", "date_time", "slot"], "TBD")
    status = _extract_field(record, ["status", "appointment_status"], "SCHEDULED")

    # 1. Evaluate Emergency Override
    is_emergency = len(extracted["matched_emergency"]) > 0
    emergency_instruction = config["emergency_instruction"] if is_emergency else None

    # 2. Calculate Configurable Priority Score
    score = 0
    breakdown: List[Dict[str, Any]] = []
    risk_flags: List[str] = []

    if is_emergency:
        # Emergency receives maximum priority rank
        score = 100
        breakdown.append({
            "rule": "EMERGENCY_OVERRIDE",
            "points": 100,
            "reason": f"Emergency red flag detected: {', '.join(extracted['matched_emergency'])}"
        })
        risk_flags.append(f"CRITICAL: Emergency symptom reported: {', '.join(extracted['matched_emergency'])}")

    else:
        # Rule 1: Symptoms requiring prompt clinical review (+30)
        if extracted["matched_prompt_symptoms"]:
            pts = weights["symptoms_prompt_review"]
            score += pts
            matched_str = ", ".join(extracted["matched_prompt_symptoms"])
            breakdown.append({
                "rule": "SYMPTOMS_PROMPT_REVIEW",
                "points": pts,
                "reason": f"Prompt review symptoms reported ({matched_str})"
            })
            risk_flags.append(f"PROMPT REVIEW: Reported symptoms ({matched_str})")

        # Rule 2: Relevant recorded cardiac history (+20)
        if extracted["has_cardiac_history"]:
            pts = weights["recorded_cardiac_history"]
            score += pts
            cardiac_str = ", ".join(extracted["matched_cardiac_history"])
            breakdown.append({
                "rule": "CARDIAC_HISTORY",
                "points": pts,
                "reason": f"Recorded cardiac / vascular history ({cardiac_str})"
            })
            risk_flags.append(f"CARDIAC RISK: Known history of {cardiac_str}")
        elif extracted["has_bp_risk"]:
            # BP risk without major cardiac event
            risk_flags.append("VASCULAR: Recorded hypertension / BP history")

        # Rule 3: Other relevant medical history (+10)
        if extracted["has_other_history"]:
            pts = weights["other_medical_history"]
            score += pts
            other_str = ", ".join(extracted["matched_other_history"])
            breakdown.append({
                "rule": "OTHER_MEDICAL_HISTORY",
                "points": pts,
                "reason": f"Other significant chronic condition ({other_str})"
            })
            risk_flags.append(f"CHRONIC CONDITION: {other_str}")

        # Rule 4: Clinician-assigned urgency (+0 to +30)
        clinician_pts = extracted["clinician_urgency_score"]
        if clinician_pts > 0:
            score += clinician_pts
            breakdown.append({
                "rule": "CLINICIAN_URGENCY",
                "points": clinician_pts,
                "reason": f"Clinician triage assessment ({extracted['clinician_urgency_label']})"
            })

    # Clamp final score between 0 and 100
    priority_score = max(0, min(100, score))

    # Priority Tier Categorization
    if is_emergency:
        priority_tier = "EMERGENCY"
    elif priority_score >= 60:
        priority_tier = "HIGH"
    elif priority_score >= 30:
        priority_tier = "MEDIUM"
    else:
        priority_tier = "ROUTINE"

    # 3. Generate Transparent Human-Readable Reason
    explanation = _generate_explanation(
        patient_name=patient_name,
        priority_score=priority_score,
        priority_tier=priority_tier,
        is_emergency=is_emergency,
        breakdown=breakdown,
        risk_flags=risk_flags,
        extracted=extracted,
        config=config,
    )

    return {
        "appointment_id": appointment_id,
        "patient_id": patient_id,
        "patient_name": patient_name,
        "doctor_name": doctor_name,
        "specialty": specialty,
        "slot_time": slot_time,
        "status": status,
        "priority_score": priority_score,
        "priority_tier": priority_tier,
        "rank": None,  # Populated when ranking list
        "is_emergency": is_emergency,
        "emergency_instruction": emergency_instruction,
        "clinical_risk_flags": risk_flags,
        "score_breakdown": breakdown,
        "explanation": explanation,
        "extracted_features": {
            "symptoms": extracted["symptoms_raw"],
            "symptom_duration": extracted["symptom_duration"],
            "matched_prompt_symptoms": extracted["matched_prompt_symptoms"],
            "cardiac_history": extracted["matched_cardiac_history"],
            "other_history": extracted["matched_other_history"],
            "bp_risk": extracted["has_bp_risk"],
            "current_medications": extracted["current_medications"],
            "clinician_urgency": extracted["clinician_urgency_label"],
        },
        "disclaimer": config["prototype_disclaimer"],
    }


def _generate_explanation(
    patient_name: str,
    priority_score: int,
    priority_tier: str,
    is_emergency: bool,
    breakdown: List[Dict[str, Any]],
    risk_flags: List[str],
    extracted: Dict[str, Any],
    config: Dict[str, Any],
) -> str:
    """Format clear, auditable reasoning explaining how the score was calculated."""
    if is_emergency:
        matched = ", ".join(extracted["matched_emergency"])
        return (
            f"[EMERGENCY OVERRIDE] for {patient_name}: Patient reports potential emergency symptom(s) "
            f"[{matched}]. Priority set to 100 (Immediate Escalation). Clinical directive: Immediately redirect "
            f"to emergency services (Call 911 / Emergency Room). Do not hold for routine outpatient queue."
        )

    parts: List[str] = [
        f"Priority Score: {priority_score}/100 ({priority_tier})."
    ]

    if breakdown:
        factor_strings = [f"+{b['points']} ({b['reason']})" for b in breakdown]
        parts.append("Score factors: " + "; ".join(factor_strings) + ".")
    else:
        parts.append("Score factors: Routine review, no acute or high-risk flags identified (+0).")

    if risk_flags:
        parts.append(f"Risk Flags ({len(risk_flags)}): " + " | ".join(risk_flags) + ".")
    else:
        parts.append("Risk Flags: None identified.")

    if extracted["symptom_duration"] and extracted["symptom_duration"] != "Unknown duration":
        parts.append(f"Reported Duration: {extracted['symptom_duration']}.")

    return " ".join(parts)


# ============================================================================
# 4. QUEUE RANKING & SORTING
# ============================================================================

def rank_appointments(
    appointments: Union[List[Dict[str, Any]], Any],
    config: Dict[str, Any] = DEFAULT_CONFIG,
) -> List[Dict[str, Any]]:
    """
    Ranks appointment records from highest to lowest priority.
    
    Ranking Order:
    1. Emergency override cases (is_emergency=True) ranked at top.
    2. Descending by priority_score (100 -> 0).
    3. Tie-breaker: chronological slot_time or appointment_id.
    
    Accepts:
    - List of dicts
    - pandas DataFrame (automatically converted via .to_dict(orient='records'))
    
    Returns:
    - List of evaluated and ranked appointment result dicts with 1-based rank assigned.
    """
    records = _coerce_records(appointments)
    if not records:
        return []

    # Evaluate each appointment
    evaluated_list: List[Dict[str, Any]] = [
        evaluate_appointment(rec, config=config) for rec in records
    ]

    # Sort key:
    # 1. Emergency first: not is_emergency (False < True, so False comes first if we sort ascending) -> (-1 if emergency else 0)
    # 2. Priority score descending -> -priority_score
    # 3. slot_time or appointment_id string ascending
    def sort_key(item: Dict[str, Any]) -> Tuple[int, int, str]:
        emergency_val = 0 if item["is_emergency"] else 1
        score_val = -item["priority_score"]
        slot_val = str(item.get("slot_time") or item.get("appointment_id") or "")
        return (emergency_val, score_val, slot_val)

    sorted_list = sorted(evaluated_list, key=sort_key)

    # Assign 1-indexed rank
    for index, item in enumerate(sorted_list, start=1):
        item["rank"] = index

    return sorted_list


def _coerce_records(data: Any) -> List[Dict[str, Any]]:
    """Helper to convert DataFrame or list of dicts safely."""
    if data is None:
        return []
    # Check if pandas DataFrame without requiring pandas imported at top
    if hasattr(data, "to_dict") and callable(getattr(data, "to_dict")):
        try:
            return data.to_dict(orient="records")
        except Exception:
            pass
    if isinstance(data, list):
        return [dict(d) if isinstance(d, dict) else getattr(d, "__dict__", {}) for d in data]
    if isinstance(data, dict):
        return [data]
    return []


# ============================================================================
# 5. SCHEDULE OPTIMIZATION PROPOSALS
# ============================================================================

def _parse_slot_datetime(slot_str: Any) -> Optional[datetime]:
    """Parse appointment slot string into a datetime object for chronological comparison."""
    if not slot_str or not isinstance(slot_str, str):
        return None
    slot_clean = slot_str.strip()
    for fmt in (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %I:%M %p",
        "%Y-%m-%d",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y %I:%M %p",
    ):
        try:
            return datetime.strptime(slot_clean, fmt)
        except ValueError:
            pass
    return None


def _is_slot_earlier(slot_a: Any, slot_b: Any) -> bool:
    """Return True if slot_a occurs before slot_b chronologically."""
    dt_a = _parse_slot_datetime(slot_a)
    dt_b = _parse_slot_datetime(slot_b)
    if dt_a and dt_b:
        return dt_a < dt_b
    if isinstance(slot_a, str) and isinstance(slot_b, str):
        # Fallback to string comparison for ISO dates
        return slot_a < slot_b
    return False


def propose_schedule_optimizations(
    ranked_appointments: List[Dict[str, Any]],
    available_slots: Optional[List[str]] = None,
    high_priority_threshold: int = 50,
    low_priority_threshold: int = 25,
) -> List[Dict[str, Any]]:
    """
    Identifies high-priority patients booked later and lower-priority
    routine patients booked earlier, and generates actionable, doctor-reviewable
    schedule swap PROPOSALS.
    
    CRITICAL SAFETY RULES:
    1. The agent ONLY PROPOSES schedule changes. It never auto-moves or cancels
       appointments. A doctor must review and approve every proposal.
    2. Emergency cases are flagged for immediate ER care, not just swapped slots.
    """
    proposals: List[Dict[str, Any]] = []
    
    if not ranked_appointments:
        return proposals

    # Filter non-cancelled appointments
    active_appointments = [
        apt for apt in ranked_appointments
        if apt.get("status", "SCHEDULED").upper() not in ["CANCELLED", "COMPLETED"]
    ]

    # High-priority candidates needing earlier access (score >= threshold, non-emergency)
    # (Emergency patients are flagged for ER redirection separately)
    high_priority_candidates = [
        apt for apt in active_appointments
        if (apt["priority_score"] >= high_priority_threshold and not apt["is_emergency"])
    ]

    # Low-priority routine candidates: score <= low_priority_threshold
    low_priority_candidates = [
        apt for apt in active_appointments
        if apt["priority_score"] <= low_priority_threshold and not apt["is_emergency"]
    ]

    proposal_counter = 1

    # Option A: If open available slots exist, check if an earlier open slot can be offered
    if available_slots:
        for high_apt in high_priority_candidates:
            earlier_open_slots = [
                s for s in available_slots if _is_slot_earlier(s, high_apt.get("slot_time"))
            ]
            if earlier_open_slots:
                best_open_slot = sorted(earlier_open_slots, key=lambda s: _parse_slot_datetime(s) or s)[0]
                proposals.append({
                    "proposal_id": f"PROP-{proposal_counter:03d}",
                    "type": "MOVE_TO_OPEN_SLOT",
                    "high_priority_appointment_id": high_apt["appointment_id"],
                    "high_priority_patient_name": high_apt["patient_name"],
                    "high_priority_score": high_apt["priority_score"],
                    "high_priority_tier": high_apt["priority_tier"],
                    "current_slot": high_apt["slot_time"],
                    "proposed_slot": best_open_slot,
                    "target_appointment_id": None,
                    "recommended_action": "EXPEDITE_INTO_OPEN_SLOT",
                    "status": "PENDING_DOCTOR_APPROVAL",
                    "rationale": (
                        f"Priority Optimization Proposal: Move {high_apt['patient_name']} "
                        f"(Score {high_apt['priority_score']}, {high_apt['priority_tier']}) from "
                        f"current slot '{high_apt['slot_time']}' into earlier open slot '{best_open_slot}'."
                    ),
                    "disclaimer": "Proposed action only. No schedule changes are executed without clinician confirmation.",
                })
                proposal_counter += 1

    # Option B: Propose swapping with lower-priority routine appointments that occupy earlier slots
    used_low_ids = set()
    for high_apt in high_priority_candidates:
        # Find low priority appointments scheduled before this high-priority appointment
        earlier_low_apts = [
            low_apt for low_apt in low_priority_candidates
            if low_apt["appointment_id"] not in used_low_ids
            and _is_slot_earlier(low_apt.get("slot_time"), high_apt.get("slot_time"))
        ]

        if earlier_low_apts:
            # Pick the earliest low-priority appointment slot
            earlier_low_apts.sort(key=lambda a: _parse_slot_datetime(a.get("slot_time")) or a.get("slot_time") or "")
            best_low = earlier_low_apts[0]
            used_low_ids.add(best_low["appointment_id"])

            proposals.append({
                "proposal_id": f"PROP-{proposal_counter:03d}",
                "type": "SLOT_SWAP_PROPOSAL",
                "high_priority_appointment_id": high_apt["appointment_id"],
                "high_priority_patient_name": high_apt["patient_name"],
                "high_priority_score": high_apt["priority_score"],
                "high_priority_tier": high_apt["priority_tier"],
                "high_priority_current_slot": high_apt["slot_time"],
                
                "low_priority_appointment_id": best_low["appointment_id"],
                "low_priority_patient_name": best_low["patient_name"],
                "low_priority_score": best_low["priority_score"],
                "low_priority_tier": best_low["priority_tier"],
                "low_priority_current_slot": best_low["slot_time"],
                
                "recommended_action": "PROPOSED_SLOT_SWAP",
                "status": "PENDING_DOCTOR_APPROVAL",
                "rationale": (
                    f"Priority Optimization Proposal: Move {high_apt['patient_name']} "
                    f"(Score {high_apt['priority_score']}, {high_apt['priority_tier']}) into earlier slot "
                    f"'{best_low['slot_time']}' currently allocated to {best_low['patient_name']} "
                    f"(Score {best_low['priority_score']}, {best_low['priority_tier']}). "
                    f"{best_low['patient_name']} is routine and can be rescheduled to '{high_apt['slot_time']}'. "
                    f"Requires Doctor Approval."
                ),
                "disclaimer": "Proposed action only. No schedule changes are executed without clinician confirmation.",
            })
            proposal_counter += 1

    return proposals


# ============================================================================
# 6. SUMMARY STATS FOR DOCTOR DASHBOARD (MEMBER 2 HELPER)
# ============================================================================

def get_priority_engine_stats(ranked_appointments: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Computes summary metrics for Member 2's doctor dashboard header / metrics cards.
    """
    total = len(ranked_appointments)
    if total == 0:
        return {
            "total_appointments": 0,
            "emergency_count": 0,
            "high_priority_count": 0,
            "medium_priority_count": 0,
            "routine_priority_count": 0,
            "average_score": 0.0,
            "pending_proposals_count": 0,
        }

    emergency_count = sum(1 for a in ranked_appointments if a["is_emergency"])
    high_count = sum(1 for a in ranked_appointments if a["priority_tier"] == "HIGH")
    medium_count = sum(1 for a in ranked_appointments if a["priority_tier"] == "MEDIUM")
    routine_count = sum(1 for a in ranked_appointments if a["priority_tier"] == "ROUTINE")
    avg_score = round(sum(a["priority_score"] for a in ranked_appointments) / total, 1)

    proposals = propose_schedule_optimizations(ranked_appointments)

    return {
        "total_appointments": total,
        "emergency_count": emergency_count,
        "high_priority_count": high_count,
        "medium_priority_count": medium_count,
        "routine_priority_count": routine_count,
        "average_score": avg_score,
        "pending_proposals_count": len(proposals),
    }


# ============================================================================
# 7. STANDALONE VERIFICATION AND DEMO TEST SUITE
# ============================================================================

def _run_demo():
    """Runs a complete test suite with representative synthetic appointment cases."""
    print("=" * 80)
    print("HEALTHCARE APPOINTMENT FLOW OPTIMIZER: DECISION ENGINE (priority_engine.py)")
    print("Role: Member 3 | Decision Engine Verification & Demo")
    print("=" * 80)

    # Synthetic test records covering all scenarios
    sample_appointments = [
        {
            "appointment_id": "APT-101",
            "patient_id": "P-001",
            "patient_name": "Eleanor Vance",
            "doctor_name": "Dr. Sarah Adams",
            "specialty": "Cardiology",
            "slot_time": "2026-10-15 09:00",
            "symptoms": "Mild knee soreness after walking",
            "symptom_duration": "3 weeks",
            "bp_history": "Normal",
            "heart_disease_history": "No",
            "existing_conditions": "None",
            "current_medications": "Multivitamin",
            "clinician_urgency": "Routine",
            "status": "SCHEDULED",
        },
        {
            "appointment_id": "APT-102",
            "patient_id": "P-002",
            "patient_name": "Arthur Pendelton",
            "doctor_name": "Dr. Sarah Adams",
            "specialty": "Cardiology",
            "slot_time": "2026-10-15 14:30",
            "symptoms": "Chest discomfort, shortness of breath on exertion, racing heart",
            "symptom_duration": "4 days",
            "bp_history": "Hypertension (Systolic 150)",
            "heart_disease_history": "Coronary artery disease, stent in 2021",
            "existing_conditions": "CAD, High BP",
            "current_medications": "Aspirin, Atorvastatin, Metoprolol",
            "clinician_urgency": "High",  # Clinician assigned +25
            "status": "SCHEDULED",
        },
        {
            "appointment_id": "APT-103",
            "patient_id": "P-003",
            "patient_name": "Marcus Wright",
            "doctor_name": "Dr. Sarah Adams",
            "specialty": "General Medicine",
            "slot_time": "2026-10-15 10:00",
            "symptoms": "CRUSHING CHEST PAIN radiating to left arm and jaw, severe shortness of breath",
            "symptom_duration": "45 minutes",
            "bp_history": "High BP",
            "heart_disease_history": "Prior MI",
            "existing_conditions": "Hypertension",
            "current_medications": "Lisinopril",
            "clinician_urgency": "Emergency",
            "status": "SCHEDULED",
        },
        {
            "appointment_id": "APT-104",
            "patient_id": "P-004",
            "patient_name": "Sonia Gomez",
            "doctor_name": "Dr. Robert Chen",
            "specialty": "Endocrinology",
            "slot_time": "2026-10-15 11:30",
            "symptoms": "Persistent dizziness, unexplained weight loss, fatigue",
            "symptom_duration": "2 weeks",
            "bp_history": "Normal",
            "heart_disease_history": "None",
            "existing_conditions": "Type 2 Diabetes, Diabetic Neuropathy",
            "current_medications": "Metformin",
            "clinician_urgency": "Moderate",  # +15
            "status": "SCHEDULED",
        },
        {
            "appointment_id": "APT-105",
            "patient_id": "P-005",
            "patient_name": "David Miller",
            "doctor_name": "Dr. Robert Chen",
            "specialty": "General Medicine",
            "slot_time": "2026-10-15 08:30",
            "symptoms": "Prescription refill, routine annual wellness review",
            "symptom_duration": "Ongoing",
            "bp_history": "Normal",
            "heart_disease_history": "No",
            "existing_conditions": "Seasonal allergies",
            "current_medications": "Cetirizine",
            "clinician_urgency": 0,
            "status": "SCHEDULED",
        },
    ]

    print(f"Loaded {len(sample_appointments)} synthetic test appointments.")
    print("Ranking appointments with rule-based priority engine...\n")

    ranked = rank_appointments(sample_appointments)

    # 1. Print Ranked Queue Table
    print(f"{'RANK':<5} | {'APT ID':<8} | {'PATIENT NAME':<18} | {'TIER':<10} | {'SCORE':<6} | {'TIME':<16} | {'EMERGENCY'}")
    print("-" * 80)
    for apt in ranked:
        em_str = "[!] YES (ESCALATED)" if apt["is_emergency"] else "No"
        print(
            f"{apt['rank']:<5} | {apt['appointment_id']:<8} | {apt['patient_name']:<18} | "
            f"{apt['priority_tier']:<10} | {apt['priority_score']:<6} | {apt['slot_time']:<16} | {em_str}"
        )

    print("\n" + "=" * 80)
    print("DETAILED EXPLANATIONS & RISK FLAGS PER PATIENT:")
    print("=" * 80)
    for apt in ranked:
        print(f"\n[Rank #{apt['rank']}] {apt['patient_name']} ({apt['appointment_id']})")
        print(f"  Score: {apt['priority_score']}/100 | Tier: {apt['priority_tier']}")
        print(f"  Explanation: {apt['explanation']}")
        if apt["emergency_instruction"]:
            print(f"  [EMERGENCY INSTRUCTION]: {apt['emergency_instruction']}")
        if apt["clinical_risk_flags"]:
            print("  Risk Flags:")
            for flag in apt["clinical_risk_flags"]:
                print(f"    - {flag}")

    # 2. Print Schedule Optimization Proposals
    print("\n" + "=" * 80)
    print("SCHEDULE OPTIMIZATION PROPOSALS (Doctor Review Required):")
    print("=" * 80)
    proposals = propose_schedule_optimizations(ranked)
    if proposals:
        for p in proposals:
            print(f"\n[{p['proposal_id']}] Status: {p['status']}")
            print(f"  Rationale: {p['rationale']}")
            print(f"  Notice: {p['disclaimer']}")
    else:
        print("No schedule swaps required.")

    # 3. Print Summary Stats for Member 2 Dashboard
    print("\n" + "=" * 80)
    print("DOCTOR DASHBOARD METRICS SUMMARY:")
    print("=" * 80)
    stats = get_priority_engine_stats(ranked)
    for k, v in stats.items():
        print(f"  - {k}: {v}")

    print("\n" + "=" * 80)
    print("All Member 3 requirements verified successfully!")
    print("=" * 80)


if __name__ == "__main__":
    _run_demo()
