import os
import time
from datetime import datetime, timezone
import io

import qrcode
from qrcode.image.svg import SvgPathImage
from flask import Blueprint, current_app, jsonify, render_template, send_from_directory, url_for, abort, Response, request, flash
from flask_login import current_user, login_required

from app_pkg.db import get_db
from app_pkg.services.security import decrypt_text, generate_secret_token
from app_pkg.routes.core import _get_health_score_data, _portal_role, _portal_theme

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
        
        # Get external URL for cross-device access using flask request context
        emergency_url = url_for('emergency.emergency_view', token=token, _external=True)
        
        print(f"QR generated successfully for: {emergency_url}")
        
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
    db = get_db()
    token_row = db.execute("SELECT * FROM emergency_tokens WHERE token = ?", (token,)).fetchone()
    if not token_row:
        abort(404)
        
    emergency_url = url_for('emergency.emergency_view', token=token, _external=True)
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(emergency_url)
    qr.make(fit=True)
    img = qr.make_image(image_factory=SvgPathImage)
    
    import io
    stream = io.BytesIO()
    img.save(stream)
    svg_data = stream.getvalue()
    
    return Response(svg_data, mimetype="image/svg+xml")


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

        # Get comprehensive medical data for embedding in QR
        files = []
        for file_row in db.execute(
            """
            SELECT filename, category, uploaded_at, doc_source
            FROM files 
            WHERE user_id = ? 
            ORDER BY uploaded_at DESC
            """,
            (user_id,),
        ).fetchall():
            d = dict(file_row)
            try:
                d["uploaded_at"] = datetime.fromtimestamp(int(file_row["uploaded_at"]), tz=timezone.utc).strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                d["uploaded_at"] = ""
            files.append(d)
        
        appointments = [dict(r) for r in db.execute(
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
        
        prescriptions = [dict(r) for r in db.execute(
            """
            SELECT medicine_name, dosage, frequency, doctor_name, start_date, end_date
            FROM prescriptions 
            WHERE user_id = ? 
            ORDER BY start_date DESC 
            LIMIT 10
            """,
            (user_id,),
        ).fetchall()]
        
        allergies_registry = [dict(r) for r in db.execute(
            """
            SELECT allergy_name, severity, notes
            FROM allergies_registry 
            WHERE user_id = ? 
            ORDER BY created_at DESC
            """,
            (user_id,),
        ).fetchall()]
        
        vitals = [dict(r) for r in db.execute(
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
        
        # Fetch detailed patient profile fields
        profile_row = db.execute(
            """
            SELECT age, gender, dob, height, weight, phone, email, address,
                   emergency_contact_name, emergency_contact_number,
                   previous_surgeries, family_medical_history, smoking_status,
                   alcohol_consumption, exercise_frequency, sleep_duration,
                   diet_preference, water_intake, occupation, preferred_language
            FROM patient_profiles
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchone()
        
        profile = dict(profile_row) if profile_row else {}
        
        # Load vaccinations
        vaccinations_rows = db.execute(
            "SELECT vaccine_name, dose_info, due_date, status FROM vaccinations WHERE user_id = ? ORDER BY due_date DESC",
            (user_id,)
        ).fetchall()
        vaccinations = [dict(v) for v in vaccinations_rows]
        
        # Load favorite doctors (first is primary doctor)
        doctors_rows = db.execute(
            "SELECT name, specialization, contact FROM doctors WHERE user_id = ? ORDER BY name ASC",
            (user_id,)
        ).fetchall()
        doctors_list = [dict(d) for d in doctors_rows]
        primary_doctor = doctors_list[0] if doctors_list else None
        
        # Fetch health score
        health_score_data = _get_health_score_data(user_id)
        health_score_val = health_score_data["value"]
        health_score_grade = "A+" if health_score_val >= 90 else ("A" if health_score_val >= 80 else ("B" if health_score_val >= 70 else ("C" if health_score_val >= 50 else "D")))

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
            user_id=user_id,
            profile=profile,
            vaccinations=vaccinations,
            primary_doctor=primary_doctor,
            health_score_val=health_score_val,
            health_score_grade=health_score_grade
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


@emergency_bp.get("/doctor/emergency-scanner")
@login_required
def doctor_emergency_scanner():
    if _portal_role(current_user.id) != "doctor":
        flash("That tool is for doctor accounts only.")
        return redirect(url_for("core.dashboard"))
        
    theme = _portal_theme(current_user.id, current_user.username)
    db = get_db()
    
    # Load recent emergency access logs for this doctor
    logs_rows = db.execute(
        """
        SELECT l.id, u.full_name as patient_name, l.accessed_at, l.reason, u.id as patient_id, t.token
        FROM emergency_access_logs l
        JOIN users u ON u.id = l.patient_user_id
        LEFT JOIN emergency_tokens t ON t.user_id = u.id AND t.expiry_time > l.accessed_at
        WHERE l.doctor_user_id = ?
        ORDER BY l.accessed_at DESC LIMIT 15
        """,
        (current_user.id,)
    ).fetchall()
    
    logs = []
    for row in logs_rows:
        try:
            pname = decrypt_text(row["patient_name"]) or "Unknown Patient"
        except Exception:
            pname = row["patient_name"] or "Unknown Patient"
            
        accessed_dt = datetime.fromtimestamp(row["accessed_at"], tz=timezone.utc).strftime("%Y-%m-%d %I:%M %p UTC")
        logs.append({
            "patient_name": pname,
            "accessed_at": accessed_dt,
            "reason": row["reason"],
            "patient_id": row["patient_id"],
            "token": row["token"] or ""
        })
        
    return render_template("doctor_emergency_scanner.html", theme=theme, logs=logs)


@emergency_bp.post("/doctor/emergency-access")
@login_required
def doctor_emergency_access():
    if _portal_role(current_user.id) != "doctor":
        return jsonify({"error": "Unauthorized. Doctor portal access required."}), 403
        
    data = request.get_json() or {}
    token = data.get("token", "").strip()
    reason = data.get("reason", "").strip() or "Emergency QR Scan"
    
    # Python fallback to parse token from URL if pasted
    if "/emergency/" in token:
        token = token.split("/emergency/")[-1]
    elif "/emergency-profile/" in token:
        token = token.split("/emergency-profile/")[-1]
    # Remove any query arguments or trail slashes
    token = token.split("?")[0].split("/")[0].strip()
    
    if not token:
        return jsonify({"error": "No emergency token or URL provided."}), 400
        
    db = get_db()
    # Check token
    row = db.execute(
        """
        SELECT t.expiry_time, u.id as patient_id, u.full_name as patient_name
        FROM emergency_tokens t
        JOIN users u ON u.id = t.user_id
        WHERE t.token = ?
        """,
        (token,),
    ).fetchone()
    
    if not row:
        return jsonify({"error": "Invalid emergency token. Patient not found."}), 404
        
    current_time = now_ts()
    if row["expiry_time"] <= current_time:
        return jsonify({"error": "Emergency link has expired. Please ask the patient to generate a new QR."}), 400
        
    # Get doctor name
    doctor_row = db.execute("SELECT full_name FROM users WHERE id = ?", (current_user.id,)).fetchone()
    doctor_name = "Dr. Unknown"
    if doctor_row and doctor_row["full_name"]:
        try:
            doctor_name = decrypt_text(doctor_row["full_name"]) or "Dr. Unknown"
        except Exception:
            doctor_name = doctor_row["full_name"]
            
    # Insert log
    db.execute(
        """
        INSERT INTO emergency_access_logs (doctor_user_id, doctor_name, patient_user_id, accessed_at, reason)
        VALUES (?, ?, ?, ?, ?)
        """,
        (current_user.id, doctor_name, row["patient_id"], now_ts(), reason),
    )
    db.commit()
    
    # Log audit entry in doctor activity logs
    try:
        db.execute(
            "INSERT INTO doctor_activity_logs (doctor_user_id, activity_type, description, created_at) VALUES (?, ?, ?, ?)",
            (current_user.id, "EMERGENCY_SCAN", f"Emergency scan executed for Patient ID {row['patient_id']}. Reason: {reason}", now_ts())
        )
        db.commit()
    except Exception:
        pass
        
    return jsonify({
        "success": True,
        "redirect_url": url_for("emergency.doctor_emergency_profile", token=token)
    })


@emergency_bp.get("/doctor/emergency-profile/<token>")
@login_required
def doctor_emergency_profile(token: str):
    if _portal_role(current_user.id) != "doctor":
        flash("Unauthorized portal role.")
        return redirect(url_for("core.dashboard"))
        
    try:
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
            flash("Patient Emergency Profile not found.")
            return redirect(url_for("emergency.doctor_emergency_scanner"))
            
        current_time = now_ts()
        expiry_time = row["expiry_time"]
        if expiry_time <= current_time:
            flash("This Emergency Link has expired.")
            return redirect(url_for("emergency.doctor_emergency_scanner"))
            
        user_id = row["user_id"]
        
        # Safe Decryption
        try:
            full_name = decrypt_text(row["full_name"]) or "Unknown Patient"
            blood_group = decrypt_text(row["blood_group"]) or "Not specified"
            allergies = decrypt_text(row["allergies"]) or "No known allergies recorded"
            medications = decrypt_text(row["medications"]) or "No current medications recorded"
            conditions = decrypt_text(row["conditions"]) or "No chronic conditions recorded"
        except Exception:
            full_name = row["full_name"] or "Unknown Patient"
            blood_group = row["blood_group"] or "Not specified"
            allergies = row["allergies"] or "No known allergies recorded"
            medications = row["medications"] or "No current medications recorded"
            conditions = row["conditions"] or "No chronic conditions recorded"
            
        # Get patient profile details
        profile_row = db.execute(
            """
            SELECT age, gender, dob, height, weight, phone, email, address,
                   emergency_contact_name, emergency_contact_number,
                   previous_surgeries, family_medical_history, smoking_status,
                   alcohol_consumption, exercise_frequency, sleep_duration,
                   diet_preference, water_intake, occupation, preferred_language, organ_donor
            FROM patient_profiles
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchone()
        
        profile = dict(profile_row) if profile_row else {}
        
        # Load files
        files = []
        for file_row in db.execute(
            "SELECT id, filename, category, uploaded_at, doc_source, file_size FROM files WHERE user_id = ? ORDER BY uploaded_at DESC",
            (user_id,),
        ).fetchall():
            d = dict(file_row)
            try:
                d["uploaded_at"] = datetime.fromtimestamp(int(file_row["uploaded_at"]), tz=timezone.utc).strftime("%Y-%m-%d")
            except Exception:
                d["uploaded_at"] = ""
            files.append(d)
            
        # Appointments
        appointments = [dict(r) for r in db.execute(
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
        
        # Prescriptions
        prescriptions = [dict(r) for r in db.execute(
            """
            SELECT medicine_name, dosage, frequency, doctor_name, start_date, end_date
            FROM prescriptions 
            WHERE user_id = ? 
            ORDER BY start_date DESC 
            """,
            (user_id,),
        ).fetchall()]
        
        # Allergies Registry
        allergies_registry = [dict(r) for r in db.execute(
            "SELECT allergy_name, severity, notes FROM allergies_registry WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()]
        
        # Vitals
        vitals = [dict(r) for r in db.execute(
            "SELECT logged_at, bp_systolic, bp_diastolic, sugar, heart_rate, weight, spo2, temperature, symptoms FROM vitals_logs WHERE user_id = ? ORDER BY logged_at DESC LIMIT 15",
            (user_id,),
        ).fetchall()]
        
        # Vaccinations
        vaccinations = [dict(v) for v in db.execute(
            "SELECT vaccine_name, dose_info, due_date, status FROM vaccinations WHERE user_id = ? ORDER BY due_date DESC",
            (user_id,)
        ).fetchall()]
        
        # Preferred / Primary Doctor
        doctors_rows = db.execute("SELECT name, specialization, contact FROM doctors WHERE user_id = ? ORDER BY name ASC", (user_id,)).fetchall()
        primary_doctor = dict(doctors_rows[0]) if doctors_rows else None
        
        # Health Score
        health_score_data = _get_health_score_data(user_id)
        health_score_val = health_score_data["value"]
        health_score_grade = "A+" if health_score_val >= 90 else ("A" if health_score_val >= 80 else ("B" if health_score_val >= 70 else ("C" if health_score_val >= 50 else "D")))
        
        # AI Summary
        ai_summary = _generate_ai_emergency_summary(
            full_name=full_name,
            blood_group=blood_group,
            allergies=allergies,
            medications=medications,
            conditions=conditions,
            files=files,
            appointments=appointments,
            prescriptions=prescriptions,
            allergies_registry=allergies_registry,
            vitals=vitals,
            profile=profile
        )
        
        theme = _portal_theme(current_user.id, current_user.username)
        
        return render_template(
            "doctor_emergency_profile.html",
            full_name=full_name,
            blood_group=blood_group,
            allergies=allergies,
            medications=medications,
            conditions=conditions,
            expires_at=datetime.fromtimestamp(expiry_time, tz=timezone.utc),
            files=files,
            appointments=appointments,
            prescriptions=prescriptions,
            allergies_registry=allergies_registry,
            vitals=vitals,
            user_id=user_id,
            profile=profile,
            vaccinations=vaccinations,
            primary_doctor=primary_doctor,
            health_score_val=health_score_val,
            health_score_grade=health_score_grade,
            token=token,
            ai_summary=ai_summary,
            theme=theme
        )
        
    except Exception as e:
        print(f"Error loading doctor emergency profile: {e}")
        import traceback
        traceback.print_exc()
        flash("An error occurred while loading the emergency profile.")
        return redirect(url_for("emergency.doctor_emergency_scanner"))


@emergency_bp.get("/doctor/emergency-preview-file/<int:file_id>")
@login_required
def doctor_emergency_preview_file(file_id: int):
    if _portal_role(current_user.id) != "doctor":
        abort(403)
        
    db = get_db()
    file_row = db.execute("SELECT user_id, filename, stored_path FROM files WHERE id = ?", (file_id,)).fetchone()
    if not file_row:
        abort(404)
        
    # Check if patient has any active token
    token_row = db.execute(
        "SELECT 1 FROM emergency_tokens WHERE user_id = ? AND expiry_time > ?",
        (file_row["user_id"], now_ts())
    ).fetchone()
    if not token_row:
        abort(403, description="Access denied. Emergency token is invalid or expired.")
        
    stored_path = file_row["stored_path"]
    
    import mimetypes
    mimetype, _ = mimetypes.guess_type(file_row["filename"])
    if not mimetype:
        mimetype = "application/octet-stream"
        
    from app_pkg.routes.core import is_supabase_configured, download_file_from_supabase
    if is_supabase_configured():
        from flask import Response
        try:
            stored_name = os.path.basename(stored_path)
            file_data = download_file_from_supabase(stored_name)
            return Response(
                file_data,
                mimetype=mimetype,
                headers={
                    "Content-Disposition": f"inline; filename={file_row['filename']}"
                }
            )
        except Exception as e:
            abort(500, description=f"Supabase download failed: {str(e)}")
            
    # Fallback to local files
    if not os.path.isabs(stored_path):
        stored_path = os.path.join(current_app.config["UPLOAD_FOLDER"], stored_path)
        
    directory = os.path.dirname(stored_path)
    filename = os.path.basename(stored_path)
    return send_from_directory(directory, filename, mimetype=mimetype, as_attachment=False)


@emergency_bp.get("/doctor/emergency-download-file/<int:file_id>")
@login_required
def doctor_emergency_download_file(file_id: int):
    if _portal_role(current_user.id) != "doctor":
        abort(403)
        
    db = get_db()
    file_row = db.execute("SELECT user_id, filename, stored_path FROM files WHERE id = ?", (file_id,)).fetchone()
    if not file_row:
        abort(404)
        
    # Check if patient has any active token
    token_row = db.execute(
        "SELECT 1 FROM emergency_tokens WHERE user_id = ? AND expiry_time > ?",
        (file_row["user_id"], now_ts())
    ).fetchone()
    if not token_row:
        abort(403, description="Access denied. Emergency token is invalid or expired.")
        
    stored_path = file_row["stored_path"]
    
    from app_pkg.routes.core import is_supabase_configured, download_file_from_supabase
    if is_supabase_configured():
        from flask import Response
        try:
            stored_name = os.path.basename(stored_path)
            file_data = download_file_from_supabase(stored_name)
            return Response(
                file_data,
                mimetype="application/octet-stream",
                headers={
                    "Content-Disposition": f"attachment; filename={file_row['filename']}"
                }
            )
        except Exception as e:
            abort(500, description=f"Supabase download failed: {str(e)}")
            
    # Fallback to local files
    if not os.path.isabs(stored_path):
        stored_path = os.path.join(current_app.config["UPLOAD_FOLDER"], stored_path)
        
    directory = os.path.dirname(stored_path)
    filename = os.path.basename(stored_path)
    return send_from_directory(directory, filename, as_attachment=True)


def _generate_ai_emergency_summary(full_name, blood_group, allergies, medications, conditions, files, appointments, prescriptions, allergies_registry, vitals, profile) -> str:
    import os
    import requests
    
    gemini_key = os.environ.get("GEMINI_API_KEY")
    
    vitals_summary = ""
    if vitals:
        vitals_summary = "\n".join([f"- {v.get('logged_at', '')}: BP {v.get('bp_systolic', 'N/A')}/{v.get('bp_diastolic', 'N/A')} mmHg, HR {v.get('heart_rate', 'N/A')} bpm, Sugar {v.get('sugar', 'N/A')} mg/dL, Temp {v.get('temperature', 'N/A')}°C, SpO2 {v.get('spo2', 'N/A')}%" for v in vitals[:5]])
        
    doc_summary = ", ".join([f"{f['filename']} ({f['category']})" for f in files[:5]])
    med_summary = ", ".join([f"{p['medicine_name']} ({p['dosage']}, {p['frequency']})" for p in prescriptions[:5]])
    allergy_summary = ", ".join([f"{a['allergy_name']} (Severity: {a['severity']})" for a in allergies_registry])
    
    prompt = (
        f"You are a clinical AI assistant summarizing patient data for an emergency doctor. "
        f"Generate a brief, clear, and highly actionable emergency clinical summary.\n\n"
        f"Patient Profile:\n"
        f"- Name: {full_name}\n"
        f"- Age/Gender: {profile.get('age', 'N/A')} / {profile.get('gender', 'N/A')}\n"
        f"- Blood Group: {blood_group}\n"
        f"- Critical Conditions: {conditions or profile.get('existing_diseases', 'None')}\n"
        f"- Allergies: {allergies or allergy_summary or 'No known allergies'}\n"
        f"- Active Medications: {medications or med_summary or 'None'}\n\n"
        f"Recent Vitals History:\n{vitals_summary or 'No vitals history'}\n\n"
        f"Uploaded Documents:\n{doc_summary or 'No documents'}\n\n"
        f"Provide the output as a bulleted summary. Focus on immediate warnings, allergies, drug interactions, or critical alerts the attending physician needs to know within 5 seconds. "
        f"Use professional clinical tone. Start directly with the summary, no intro/outro conversational fluff. Keep it under 200 words."
    )
    
    if gemini_key:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
            headers = {"content-type": "application/json"}
            payload = {
                "contents": [{"parts": [{"text": prompt}]}]
            }
            res = requests.post(url, headers=headers, json=payload, timeout=10)
            if res.status_code == 200:
                text = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                if text:
                    return text
        except Exception as e:
            print(f"Gemini emergency summary failed: {e}")
            
    # Fallback rule-based
    fallback = []
    if allergies and allergies != "No known allergies recorded" and allergies != "Unknown":
        fallback.append(f"⚠️ **Allergies:** {allergies}")
    if allergy_summary:
        fallback.append(f"⚠️ **Allergy Registry:** {allergy_summary}")
    if conditions and conditions != "No chronic conditions recorded" and conditions != "Unknown":
        fallback.append(f"🚨 **Chronic Diseases & Conditions:** {conditions}")
    elif profile.get('existing_diseases'):
        fallback.append(f"🚨 **Chronic Diseases & Conditions:** {profile.get('existing_diseases')}")
        
    if medications and medications != "No current medications recorded" and medications != "Unknown":
        fallback.append(f"💊 **Medications:** {medications}")
    elif med_summary:
        fallback.append(f"💊 **Medications (Active Prescriptions):** {med_summary}")
        
    if vitals:
        latest = vitals[0]
        latest_vital_warnings = []
        if latest.get('bp_systolic') and int(latest['bp_systolic']) > 140:
            latest_vital_warnings.append(f"Elevated Blood Pressure: {latest['bp_systolic']}/{latest['bp_diastolic']} mmHg")
        if latest.get('heart_rate') and int(latest['heart_rate']) > 100:
            latest_vital_warnings.append(f"Tachycardia (HR: {latest['heart_rate']} bpm)")
        if latest.get('spo2') and int(latest['spo2']) < 95:
            latest_vital_warnings.append(f"Low Oxygen Saturation (SpO2: {latest['spo2']}%)")
        if latest_vital_warnings:
            fallback.append("📈 **Vitals Warnings:** " + "; ".join(latest_vital_warnings))
            
    if not fallback:
        return "Patient has no critical alerts, chronic conditions, or medications recorded. Please interview patient or family directly."
        
    return "\n\n".join(fallback)


