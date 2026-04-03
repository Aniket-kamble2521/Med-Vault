"""Rule-based clinical hints for the AI Health Assistant (not a medical device; educational only)."""

from __future__ import annotations


def suggest_from_symptoms(symptoms: str, age_years: int | None = None, patient_data=None, recent_files=None, recent_appointments=None, current_prescriptions=None) -> dict[str, object]:
    text = (symptoms or "").lower().strip()
    diagnoses: list[str] = []
    tests: list[str] = []
    age_note = ""
    personalized_note = ""
    
    if age_years is not None and age_years >= 65:
        age_note = "Patient is older adult — consider broader differential and medication review."

    # Add personalized context based on patient data
    if patient_data:
        try:
            blood_group = patient_data.get('blood_group', '') or ''
            allergies = (patient_data.get('allergies', '') or '').lower()
            medications = (patient_data.get('medications', '') or '').lower()
            conditions = (patient_data.get('conditions', '') or '').lower()
            
            # Personalized notes based on medical history
            if allergies and 'penicillin' in allergies and any(k in text for k in ('infection', 'antibiotic', 'fever')):
                personalized_note += "⚠️ Patient has penicillin allergy - consider alternative antibiotics. "
            
            if 'diabetes' in conditions and any(k in text for k in ('thirsty', 'urinate', 'sugar', 'glucose')):
                personalized_note += "🔬 Consider checking blood sugar levels - patient has diabetes history. "
            
            if 'asthma' in conditions and any(k in text for k in ('breath', 'wheeze', 'cough', 'shortness')):
                personalized_note += "🫁 Patient has asthma - consider inhaler use and peak flow measurement. "
                
            if medications and any(k in text for k in ('headache', 'pain', 'fever')):
                personalized_note += "💊 Review current medications before adding new pain relievers. "
        except Exception:
            pass  # Skip personalized notes if data is invalid

    def add(dx: str, t: list[str]) -> None:
        if dx not in diagnoses:
            diagnoses.append(dx)
        for x in t:
            if x not in tests:
                tests.append(x)

    # Enhanced symptom analysis with patient context
    if any(k in text for k in ("chest", "heart", "pressure", "angina")):
        add(
            "Chest discomfort: consider ACS, GERD, costochondritis, anxiety; rule out life threats first.",
            ["12-lead ECG", "Troponin serial testing", "CXR if indicated"],
        )
    if any(k in text for k in ("fever", "chills", "sepsis")):
        add("Systemic infection possible: correlate with exam and vitals.", ["CBC", "CMP", "Blood cultures if septic"])
    if any(k in text for k in ("headache", "migraine")):
        add("Headache: distinguish primary vs. secondary; red flags for thunderclap or neuro deficit.", ["Neurological exam", "CT head if sudden severe or focal signs"])
    if any(k in text for k in ("cough", "sob", "shortness", "breath")):
        add("Respiratory symptoms: viral URI vs. pneumonia vs. asthma/COPD exacerbation.", ["SpO2", "CXR if focal findings or hypoxia"])
    if any(k in text for k in ("abdomen", "stomach", "nausea", "vomit")):
        add("Abdominal pain: broad differential by location and exam.", ["CBC", "Lipase if epigastric", "UA if flank pain"])
    if any(k in text for k in ("diabetes", "glucose", "sugar", "hba1c")):
        add("Glycemic concerns: confirm control and complications screening.", ["HbA1c", "CMP", "Urinalysis"])
    
    # Patient-specific recommendations
    if current_prescriptions and len(current_prescriptions) > 0:
        add("Review current medications for potential interactions or side effects.", ["Medication review"])
    
    if recent_files and len(recent_files) > 0:
        add("Consider recent medical reports in your evaluation.", ["Review recent lab results"])
    
    if not diagnoses:
        add(
            "Insufficient specificity from text alone — expand history, vitals, and focused exam.",
            ["Directed labs and imaging based on exam"],
        )

    # Patient-focused disclaimer
    disclaimer = (
        "These suggestions are for educational purposes only and are not medical advice. "
        "Always consult with your healthcare provider for proper diagnosis and treatment. "
        "In case of emergency, seek immediate medical attention."
    )

    return {
        "diagnoses": diagnoses[:6],
        "tests": tests[:10],
        "age_note": age_note,
        "personalized_note": personalized_note,
        "disclaimer": disclaimer,
    }
