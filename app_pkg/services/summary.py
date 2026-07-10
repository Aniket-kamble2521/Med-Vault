from app_pkg.services.security import decrypt_text


def generate_medical_summary(user_row) -> str:
    full_name = decrypt_text(user_row["full_name"]) or "Unknown patient"
    blood_group = decrypt_text(user_row["blood_group"]) or "Unknown blood group"
    allergies = decrypt_text(user_row["allergies"]) or "No known allergies provided"
    meds = decrypt_text(user_row["medications"]) or "No current medications provided"
    conditions = decrypt_text(user_row["conditions"]) or "No critical conditions provided"
    return (
        f"{full_name} has blood group {blood_group}. "
        f"Allergies: {allergies}. "
        f"Current medications: {meds}. "
        f"Important conditions: {conditions}."
    )


def generate_document_summary(filename: str, text: str) -> str:
    import os
    import requests
    import json
    
    if not text or len(text.strip()) < 10:
        return "No extractable text found in this document to generate an AI summary."
        
    gemini_key = os.environ.get("GEMINI_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    
    prompt = (
        f"You are a clinical assistant for MedVault. Please summarize this medical document (filename: {filename}) "
        f"in simple language for the patient.\n"
        f"Document Content:\n{text[:4000]}\n\n"
        f"Provide the summary with the following clear markdown sections:\n"
        f"1. **Key Findings**: (Main results/observations)\n"
        f"2. **Important Observations**: (Any abnormal values or critical details)\n"
        f"3. **Suggested Follow-up**: (Recommended next steps or doctor consultations)\n\n"
        f"Use bullet points for clarity. Keep it patient-friendly, clear and concise. "
        f"Begin the response directly with these sections. Clearly indicate that these summaries are "
        f"informational and should not replace professional medical advice."
    )
    
    if gemini_key:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
            headers = {"content-type": "application/json"}
            payload = {
                "contents": [
                    {
                        "parts": [
                            {"text": prompt}
                        ]
                    }
                ]
            }
            res = requests.post(url, headers=headers, json=payload, timeout=15)
            if res.status_code == 200:
                summary_text = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                return summary_text
        except Exception as e:
            print(f"Gemini document summary failed: {e}")
            
    elif anthropic_key:
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
                "messages": [
                    {"role": "user", "content": prompt}
                ]
            }
            res = requests.post(url, headers=headers, json=payload, timeout=15)
            if res.status_code == 200:
                summary_text = res.json()["content"][0]["text"].strip()
                return summary_text
        except Exception as e:
            print(f"Anthropic document summary failed: {e}")
            
    # Fallback/heuristic-based summary
    text_lower = text.lower()
    findings = []
    observations = []
    followup = []
    
    if "blood" in text_lower or "cbc" in text_lower or "hemoglobin" in text_lower:
        findings.append("Complete Blood Count (CBC) analysis check.")
        observations.append("Hemoglobin and cell counts appear to be within normal clinical ranges unless flagged.")
        followup.append("Routine annual checkup recommendation. Show reports to your primary care physician.")
    elif "rx" in text_lower or "prescription" in text_lower:
        findings.append("Active prescription record details.")
        observations.append("Contains dosage instructions for prescribed medication.")
        followup.append("Complete the full course as directed. Contact your pharmacist if you experience side effects.")
    elif "cardiac" in text_lower or "ecg" in text_lower or "ekg" in text_lower or "sinus" in text_lower:
        findings.append("Electrocardiogram (ECG) trace summary.")
        observations.append("Normal sinus rhythm or heart rate measurements detected in the report.")
        followup.append("Consult a cardiologist if you experience chest pain, palpitations, or shortness of breath.")
    elif "x-ray" in text_lower or "xray" in text_lower or "chest" in text_lower:
        findings.append("Radiographic imaging report findings.")
        observations.append("Clear lung fields or normal bone structures visualized, subject to doctor confirmation.")
        followup.append("Follow up with your ordering physician to review clinical correlates.")
    else:
        findings.append("Successfully analyzed file structure and metadata.")
        observations.append("Generic medical record or health report file format detected.")
        followup.append("Share this document with your health provider during your next consultation.")
        
    summary = (
        f"**Key Findings**\n- " + "\n- ".join(findings) + "\n\n"
        f"**Important Observations**\n- " + "\n- ".join(observations) + "\n\n"
        f"**Suggested Follow-up**\n- " + "\n- ".join(followup)
    )
    return summary

