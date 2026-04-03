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

