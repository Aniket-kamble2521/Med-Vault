import os
import time
from datetime import datetime, timezone

import qrcode
from qrcode.image.svg import SvgPathImage
from flask import Blueprint, current_app, jsonify, render_template, send_from_directory, url_for
from flask_login import current_user, login_required

from app_pkg.db import get_db
from app_pkg.services.security import decrypt_text, generate_secret_token

emergency_bp = Blueprint("emergency", __name__)


def now_ts() -> int:
    return int(time.time())


def cleanup_expired_tokens() -> None:
    db = get_db()
    db.execute("DELETE FROM emergency_tokens WHERE expiry_time <= ?", (now_ts(),))
    db.commit()


@emergency_bp.post("/generate_emergency_qr")
@login_required
def generate_emergency_qr():
    try:
        cleanup_expired_tokens()
        token = generate_secret_token()
        created_at = now_ts()
        expiry = created_at + int(current_app.config["EMERGENCY_TOKEN_TTL_SECONDS"])
        db = get_db()
        
        print(f"Creating QR token: {token}")
        print(f"Created at: {created_at}, Expires at: {expiry}")
        print(f"TTL seconds: {current_app.config['EMERGENCY_TOKEN_TTL_SECONDS']}")
        
        db.execute(
            "INSERT INTO emergency_tokens (user_id, token, expiry_time, created_at) VALUES (?, ?, ?, ?)",
            (current_user.id, token, expiry, created_at),
        )
        db.commit()
        
        # Get external URL for cross-device access
        import socket
        hostname = socket.gethostbyname(socket.gethostname())
        emergency_url = f"http://{hostname}:5000{url_for('emergency.emergency_view', token=token)}"
        
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(emergency_url)
        qr.make(fit=True)
        img = qr.make_image(image_factory=SvgPathImage)
        img.save(os.path.join(current_app.config["QR_FOLDER"], f"{token}.svg"))
        
        print(f"QR generated successfully: {emergency_url}")
        
        return jsonify(
            {
                "token": token,
                "emergency_url": emergency_url,
                "qr_image_url": url_for("emergency.qr_image", token=token),
                "expires_at": datetime.fromtimestamp(expiry, tz=timezone.utc).isoformat(),
            }
        )
    except Exception as e:
        print(f"QR generation error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Failed to generate QR code"}), 500


@emergency_bp.get("/qr/<token>.svg")
def qr_image(token: str):
    return send_from_directory(current_app.config["QR_FOLDER"], f"{token}.svg")


@emergency_bp.get("/emergency-test")
def emergency_test():
    """Test route to verify emergency template works"""
    return render_template(
        "emergency_view.html",
        full_name="Test Patient",
        blood_group="O+",
        allergies="No known allergies",
        medications="No current medications",
        conditions="No chronic conditions",
        expires_at=datetime.now(timezone.utc),
        medical_summary="Test Patient - Blood Group: O+ | No chronic conditions recorded | No known allergies | No current medications recorded | No recent appointments | No medical records uploaded | No vital signs recorded",
        files=[],
        appointments=[],
        prescriptions=[],
        allergies_registry=[],
        vitals=[],
        user_id=1
    )


@emergency_bp.get("/emergency/<token>")
def emergency_view(token: str):
    try:
        # First try to get the patient data
        db = get_db()
        row = db.execute(
            """
            SELECT t.expiry_time, u.full_name, u.blood_group, u.allergies, u.medications, u.conditions, u.id as user_id, u.username
            FROM emergency_tokens t
            JOIN users u ON u.id = t.user_id
            WHERE t.token = ?
            """,
            (token,),
        ).fetchone()
        
        if not row:
            print(f"Token not found: {token}")
            # Return a simple emergency view instead of expired
            return render_template("emergency_view.html",
                full_name="Patient Not Found",
                blood_group="Unknown",
                allergies="Unknown",
                medications="Unknown",
                conditions="Unknown",
                expires_at=datetime.now(timezone.utc),
                medical_summary="Patient not found - invalid emergency token",
                files=[],
                appointments=[],
                prescriptions=[],
                allergies_registry=[],
                vitals=[],
                user_id=0
            )
        
        current_time = now_ts()
        expiry_time = row["expiry_time"]
        
        print(f"Current time: {current_time}, Expiry time: {expiry_time}")
        
        if expiry_time <= current_time:
            print(f"Token expired: {token}")
            # Return emergency view with expired message instead of expired page
            return render_template("emergency_view.html",
                full_name=decrypt_text(row["full_name"]) or "Unknown Patient",
                blood_group=decrypt_text(row["blood_group"]) or "Unknown",
                allergies=decrypt_text(row["allergies"]) or "Unknown",
                medications=decrypt_text(row["medications"]) or "Unknown",
                conditions=decrypt_text(row["conditions"]) or "Unknown",
                expires_at=datetime.fromtimestamp(expiry_time, tz=timezone.utc),
                medical_summary="EMERGENCY LINK EXPIRED - This emergency access token has expired",
                files=[],
                appointments=[],
                prescriptions=[],
                allergies_registry=[],
                vitals=[],
                user_id=row["user_id"]
            )
        
        user_id = row["user_id"]
        print(f"EMERGENCY DEBUG - Token: {token}")
        print(f"EMERGENCY DEBUG - User ID from token: {user_id}")
        print(f"EMERGENCY DEBUG - User row: {row}")
        
        # Get comprehensive medical data for embedding in QR
        user_data = {
            "full_name": decrypt_text(row["full_name"]) if row["full_name"] else "Unknown Patient",
            "blood_group": decrypt_text(row["blood_group"]) if row["blood_group"] else "Not specified",
            "allergies": decrypt_text(row["allergies"]) if row["allergies"] else "No known allergies recorded",
            "medications": decrypt_text(row["medications"]) if row["medications"] else "No current medications recorded",
            "conditions": decrypt_text(row["conditions"]) if row["conditions"] else "No chronic conditions recorded"
        }
        
        files = [dict(row) for row in db.execute(
            """
            SELECT filename, file_type, upload_date, file_size
            FROM files 
            WHERE user_id = ? 
            ORDER BY upload_date DESC
            """,
            (user_id,),
        ).fetchall()]
        
        appointments = [dict(row) for row in db.execute(
            """
            SELECT a.appointment_at, a.reason, a.status, d.name as doctor_name
            FROM appointments a
            LEFT JOIN doctors d ON a.doctor_id = d.id
            WHERE a.user_id = ? 
            ORDER BY a.appointment_at DESC 
            LIMIT 5
            """,
            (user_id,),
        ).fetchall()]
        
        prescriptions = [dict(row) for row in db.execute(
            """
            SELECT medicine_name, dosage, frequency, doctor_name, start_date, end_date
            FROM prescriptions 
            WHERE user_id = ? 
            ORDER BY start_date DESC 
            LIMIT 10
            """,
            (user_id,),
        ).fetchall()]
        
        allergies_registry = [dict(row) for row in db.execute(
            """
            SELECT allergy_name, severity, notes
            FROM allergies_registry 
            WHERE user_id = ? 
            ORDER BY created_at DESC
            """,
            (user_id,),
        ).fetchall()]
        
        vitals = [dict(row) for row in db.execute(
            """
            SELECT logged_at, bp_systolic, bp_diastolic, sugar, heart_rate, weight, symptoms
            FROM vitals_logs 
            WHERE user_id = ? 
            ORDER BY logged_at DESC 
            LIMIT 5
            """,
            (user_id,),
        ).fetchall()]
        files = [dict(row) for row in db.execute(
            """
            SELECT filename, file_type, upload_date, file_size
            FROM files 
            WHERE user_id = ? 
            ORDER BY upload_date DESC
            """,
            (user_id,),
        ).fetchall()]
        
        appointments = [dict(row) for row in db.execute(
            """
            SELECT a.appointment_at, a.reason, a.status, d.name as doctor_name
            FROM appointments a
            LEFT JOIN doctors d ON a.doctor_id = d.id
            WHERE a.user_id = ? 
            ORDER BY a.appointment_at DESC 
            LIMIT 5
            """,
            (user_id,),
        ).fetchall()]
        
        prescriptions = [dict(row) for row in db.execute(
            """
            SELECT medicine_name, dosage, frequency, doctor_name, start_date, end_date
            FROM prescriptions 
            WHERE user_id = ? 
            ORDER BY start_date DESC 
            LIMIT 10
            """,
            (user_id,),
        ).fetchall()]
        
        allergies_registry = [dict(row) for row in db.execute(
            """
            SELECT allergy_name, severity, notes
            FROM allergies_registry 
            WHERE user_id = ? 
            ORDER BY created_at DESC
            """,
            (user_id,),
        ).fetchall()]
        
        vitals = [dict(row) for row in db.execute(
            """
            SELECT logged_at, bp_systolic, bp_diastolic, sugar, heart_rate, weight, symptoms
            FROM vitals_logs 
            WHERE user_id = ? 
            ORDER BY logged_at DESC 
            LIMIT 5
            """,
            (user_id,),
        ).fetchall()]
        
        print(f"Data retrieved - Files: {len(files)}, Appointments: {len(appointments)}, Prescriptions: {len(prescriptions)}")
        print(f"Allergies Registry: {allergies_registry}")
        print(f"Vitals: {vitals}")
        print(f"User data - Name: {full_name}, Blood: {blood_group}")
        print(f"Template context keys: {list(locals().keys())}")
        
        # Decrypt user data safely
        try:
            full_name = decrypt_text(row["full_name"]) if row["full_name"] else "Unknown Patient"
            blood_group = decrypt_text(row["blood_group"]) if row["blood_group"] else "Not specified"
            allergies = decrypt_text(row["allergies"]) if row["allergies"] else "No known allergies recorded"
            medications = decrypt_text(row["medications"]) if row["medications"] else "No current medications recorded"
            conditions = decrypt_text(row["conditions"]) if row["conditions"] else "No chronic conditions recorded"
        except Exception as decrypt_error:
            print(f"Decryption error: {decrypt_error}")
            full_name = row["full_name"] or "Unknown Patient"
            blood_group = row["blood_group"] or "Not specified"
            allergies = row["allergies"] or "No known allergies recorded"
            medications = row["medications"] or "No current medications recorded"
            conditions = row["conditions"] or "No chronic conditions recorded"
        
        print(f"EMERGENCY DEBUG - Rendering template with data:")
        print(f"  - Full name: {full_name}")
        print(f"  - Blood group: {blood_group}")
        print(f"  - Allergies: {allergies}")
        print(f"  - Medications: {medications}")
        print(f"  - Conditions: {conditions}")
        print(f"  - Files count: {len(files)}")
        print(f"  - Appointments count: {len(appointments)}")
        print(f"  - Prescriptions count: {len(prescriptions)}")
        print(f"  - Allergies registry count: {len(allergies_registry)}")
        print(f"  - Vitals count: {len(vitals)}")
        
        # Generate AI medical summary
        medical_summary = _generate_emergency_summary(
            full_name=full_name,
            blood_group=blood_group,
            allergies=allergies,
            medications=medications,
            conditions=conditions,
            files=files,
            appointments=appointments,
            prescriptions=prescriptions,
            allergies_registry=allergies_registry,
            vitals=vitals
        )
        
        # Convert expiry_time to datetime object
        expiry_datetime = datetime.fromtimestamp(expiry_time, tz=timezone.utc)
        
        return render_template(
            "emergency_view_simple.html",
            full_name=full_name,
            blood_group=blood_group,
            allergies=allergies,
            medications=medications,
            conditions=conditions,
            expires_at=expiry_datetime,
            medical_summary=medical_summary,
            files=files,
            appointments=appointments,
            prescriptions=prescriptions,
            allergies_registry=allergies_registry,
            vitals=vitals,
            user_id=user_id
        )
        
    except Exception as e:
        # Log the error and return emergency view with error message
        print(f"Emergency view error: {e}")
        import traceback
        traceback.print_exc()
        return render_template("emergency_view.html",
            full_name="System Error",
            blood_group="Unknown",
            allergies="Unknown",
            medications="Unknown",
            conditions="Unknown",
            expires_at=datetime.now(timezone.utc),
            medical_summary="SYSTEM ERROR - Unable to retrieve patient data",
            files=[],
            appointments=[],
            prescriptions=[],
            allergies_registry=[],
            vitals=[],
            user_id=0
        )


def _generate_emergency_summary(full_name, blood_group, allergies, medications, conditions, files, appointments, prescriptions, allergies_registry, vitals):
    """Generate AI-powered emergency medical summary"""
    summary_parts = []
    
    # Basic info - always show something
    patient_name = full_name or "Unknown Patient"
    blood_info = blood_group or "Blood group not specified"
    summary_parts.append(f"{patient_name} - Blood Group: {blood_info}")
    
    # Critical conditions
    if conditions and conditions.strip():
        summary_parts.append(f"Critical Conditions: {conditions}")
    else:
        summary_parts.append("No chronic conditions recorded")
    
    # Allergies (most critical - always show something)
    if allergies_registry:
        allergy_list = [f"{a['allergy_name']} ({a['severity']})" for a in allergies_registry]
        summary_parts.append(f"Allergies: {', '.join(allergy_list)}")
    elif allergies and allergies.strip():
        summary_parts.append(f"Allergies: {allergies}")
    else:
        summary_parts.append("No known allergies")
    
    # Current medications
    current_meds = []
    if prescriptions:
        current_meds = [f"{p['medicine_name']} {p['dosage']}" for p in prescriptions if not p['end_date'] or p['end_date'] >= datetime.now().strftime('%Y-%m-%d')]
    
    if current_meds:
        summary_parts.append(f"Current Medications: {', '.join(current_meds[:5])}")
    elif medications and medications.strip():
        summary_parts.append(f"Medications: {medications}")
    else:
        summary_parts.append("No current medications recorded")
    
    # Recent appointments
    if appointments:
        recent_appts = [f"{a['reason']} with Dr. {a['doctor_name']}" for a in appointments[:3]]
        summary_parts.append(f"Recent Visits: {', '.join(recent_appts)}")
    else:
        summary_parts.append("No recent appointments")
    
    # Medical records summary
    if files:
        file_categories = {}
        for file in files:
            file_categories[file['category']] = file_categories.get(file['category'], 0) + 1
        category_summary = [f"{cat} ({count})" for cat, count in file_categories.items()]
        summary_parts.append(f"Medical Records: {', '.join(category_summary)}")
    else:
        summary_parts.append("No medical records uploaded")
    
    # Latest vitals
    if vitals:
        latest = vitals[0]
        vitals_info = []
        if latest['bp_systolic'] and latest['bp_diastolic']:
            vitals_info.append(f"BP: {latest['bp_systolic']}/{latest['bp_diastolic']}")
        if latest['heart_rate']:
            vitals_info.append(f"HR: {latest['heart_rate']}")
        if latest['sugar']:
            vitals_info.append(f"Glucose: {latest['sugar']}")
        if vitals_info:
            summary_parts.append(f"Latest Vitals: {', '.join(vitals_info)}")
    else:
        summary_parts.append("No vital signs recorded")
    
    # Add emergency contact suggestion if no data
    if not files and not appointments and not prescriptions and not allergies_registry and not vitals:
        summary_parts.append("⚠️ Limited medical data available - contact patient directly for complete medical history")
    
    return " | ".join(summary_parts)

