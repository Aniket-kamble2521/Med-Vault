"""Rule-based clinical hints for the AI Health Assistant (not a medical device; educational only)."""

from __future__ import annotations


def _get_val(row, key, default=""):
    if row is None:
        return default
    if hasattr(row, "get"):
        try:
            return row.get(key, default)
        except Exception:
            pass
    try:
        return row[key]
    except Exception:
        return default


def suggest_from_symptoms(symptoms: str, age_years: int | None = None, patient_data=None, recent_files=None, recent_appointments=None, current_prescriptions=None) -> dict[str, object]:
    import os
    import json
    import requests

    # Construct unified LLM prompt with patient context
    prompt_parts = [f"Reported Symptoms: {symptoms}"]
    if age_years is not None:
        prompt_parts.append(f"Patient Age: {age_years} years old")
    
    if patient_data:
        try:
            blood_group = _get_val(patient_data, 'blood_group', '')
            allergies = _get_val(patient_data, 'allergies', '')
            medications = _get_val(patient_data, 'medications', '')
            conditions = _get_val(patient_data, 'conditions', '')
            if blood_group or allergies or medications or conditions:
                prompt_parts.append("Patient Medical History:")
                if blood_group: prompt_parts.append(f"  - Blood Group: {blood_group}")
                if allergies: prompt_parts.append(f"  - Known Allergies: {allergies}")
                if medications: prompt_parts.append(f"  - Current Medications: {medications}")
                if conditions: prompt_parts.append(f"  - Medical Conditions: {conditions}")
        except Exception:
            pass

    if recent_files:
        files_str = ", ".join([_get_val(f, "filename", "") for f in recent_files if _get_val(f, "filename")])
        if files_str:
            prompt_parts.append(f"Recent Uploaded Medical Records: {files_str}")

    if recent_appointments:
        appts_str = ", ".join([_get_val(f, "reason", "") for f in recent_appointments if _get_val(f, "reason")])
        if appts_str:
            prompt_parts.append(f"Recent Consultations/Visits: {appts_str}")

    if current_prescriptions:
        rx_str = ", ".join([_get_val(f, "medicine_name", "") for f in current_prescriptions if _get_val(f, "medicine_name")])
        if rx_str:
            prompt_parts.append(f"Active Prescriptions: {rx_str}")

    prompt = "\n".join(prompt_parts)

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")

    if anthropic_key:
        try:
            url = "https://api.anthropic.com/v1/messages"
            headers = {
                "x-api-key": anthropic_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            }
            payload = {
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1024,
                "system": "You are a clinical decision support assistant. Analyze symptoms and patient history. Respond ONLY with a JSON object containing the keys: 'diagnoses' (list of strings), 'tests' (list of strings), 'age_note' (string), 'personalized_note' (string), 'disclaimer' (string). Do not include any markdown syntax, code blocks, or explanations.",
                "messages": [
                    {"role": "user", "content": prompt}
                ]
            }
            res = requests.post(url, headers=headers, json=payload, timeout=12)
            if res.status_code == 200:
                content = res.json()["content"][0]["text"].strip()
                if content.startswith("```json"):
                    content = content[7:]
                if content.endswith("```"):
                    content = content[:-3]
                return json.loads(content.strip())
        except Exception as e:
            print(f"Anthropic API call failed: {e}")

    elif gemini_key:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
            headers = {"content-type": "application/json"}
            payload = {
                "contents": [
                    {
                        "parts": [
                            {"text": "Analyze the following patient symptoms and history:\n" + prompt + "\n\nYou are a clinical decision support assistant. Respond with a JSON object containing keys: 'diagnoses' (list of strings), 'tests' (list of strings), 'age_note' (string), 'personalized_note' (string), 'disclaimer' (string)."}
                        ]
                    }
                ],
                "generationConfig": {
                    "responseMimeType": "application/json"
                }
            }
            res = requests.post(url, headers=headers, json=payload, timeout=12)
            if res.status_code == 200:
                content = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                return json.loads(content)
        except Exception as e:
            print(f"Gemini API call failed: {e}")

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
            blood_group = _get_val(patient_data, 'blood_group', '') or ''
            allergies = (_get_val(patient_data, 'allergies', '') or '').lower()
            medications = (_get_val(patient_data, 'medications', '') or '').lower()
            conditions = (_get_val(patient_data, 'conditions', '') or '').lower()
            
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
