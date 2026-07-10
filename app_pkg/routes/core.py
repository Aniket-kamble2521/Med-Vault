import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_from_directory, url_for, jsonify
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from app_pkg.db import get_db
from app_pkg.services.doctor_ai import suggest_from_symptoms
from app_pkg.services.files import ai_categorize_medical_file
from app_pkg.services.ocr import extract_text_with_tesseract
from app_pkg.services.security import decrypt_text, encrypt_text, hash_password, verify_password
from app_pkg.services.summary import generate_medical_summary, generate_document_summary
from app_pkg.services.supabase_storage import (
    is_supabase_configured,
    upload_file_to_supabase,
    download_file_from_supabase,
    delete_file_from_supabase,
)

core_bp = Blueprint("core", __name__)

ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}


def now_ts() -> int:
    return int(time.time())


def allowed_file(filename: str) -> bool:
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_EXTENSIONS


def _portal_role(user_id: int) -> str:
    db = get_db()
    row = db.execute("SELECT portal_role FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row or not row["portal_role"]:
        return "patient"
    return str(row["portal_role"]).strip().lower() or "patient"


def _normalize_accent(raw: str) -> str:
    raw = (raw or "").strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", raw):
        return raw
    return "#3b82f6"


def _hex_to_rgb_css(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return "59, 130, 246"
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"{r}, {g}, {b}"
    except ValueError:
        return "59, 130, 246"


@core_bp.before_app_request
def enforce_role_protection():
    if request.path == "/logout" or request.endpoint in ("auth.logout", "static") or (request.endpoint and request.endpoint.startswith("static")):
        return

    is_public = False
    if request.endpoint in ("core.index", "auth.login", "auth.register"):
        is_public = True
    elif request.path.startswith("/emergency/") or request.path.startswith("/qr/"):
        is_public = True

    if is_public:
        return

    if not current_user or not current_user.is_authenticated:
        return

    role = getattr(current_user, "portal_role", "patient")

    is_doctor_route = False
    if request.path.startswith("/doctor"):
        is_doctor_route = True
    elif request.endpoint and request.endpoint.startswith("core.doctor_"):
        is_doctor_route = True

    is_patient_route = False
    patient_paths = ["/dashboard", "/analytics", "/ai-assistant", "/book-appointment", "/profile", "/upload", "/files/", "/health-vitals", "/generate_emergency_qr", "/emergency-test", "/health-score", "/health-trends", "/medicine-reminders", "/health-timeline", "/medical-documents", "/favorite-doctors", "/emergency-info", "/api/patient", "/patient/payments", "/change-password", "/patient/appointments", "/health-goals"]
    if any(request.path.startswith(p) for p in patient_paths):
        is_patient_route = True
    elif request.endpoint and request.endpoint.startswith("modules."):
        is_patient_route = True

    if is_doctor_route and role != "doctor":
        abort(403)
    elif is_patient_route and role != "patient":
        abort(403)


@core_bp.app_errorhandler(403)
def forbidden_error(e):
    return render_template("403.html"), 403


@core_bp.before_app_request
def check_onboarding():
    if current_user and current_user.is_authenticated:
        if request.path == "/logout" or request.endpoint in ("core.onboarding", "core.complete_onboarding", "auth.logout", "static") or (request.endpoint and request.endpoint.startswith("static")):
            return
        
        db = get_db()
        row = db.execute("SELECT onboarding_done, portal_role FROM users WHERE id = ?", (int(current_user.id),)).fetchone()
        if row and not row["onboarding_done"]:
            return redirect(url_for("core.onboarding"))


def _portal_theme(user_id: int, username: str) -> dict:
    db = get_db()
    row = db.execute(
        """
        SELECT portal_role, theme_accent, theme_mode, full_name, doctor_specialty
        FROM users WHERE id = ?
        """,
        (user_id,),
    ).fetchone()
    portal = (row["portal_role"] or "patient").strip().lower() if row else "patient"
    accent = _normalize_accent(row["theme_accent"] if row else "")
    if portal == "patient" and accent.lower() in ("#3b82f6", "#2563eb"):
        accent = "#334155"
    mode = (row["theme_mode"] or "light").strip().lower() if row else "light"
    if mode not in ("light", "dark"):
        mode = "light"
    fn = (decrypt_text(row["full_name"]) if row else "") or ""
    fn = fn.strip()
    display_name = fn or username
    if portal == "doctor" and not display_name.lower().startswith("dr"):
        display_name = f"Dr. {display_name}"
    
    if portal == "doctor":
        # Check doctor_profiles first
        prof_row = db.execute("SELECT specialty FROM doctor_profiles WHERE user_id = ?", (user_id,)).fetchone()
        if prof_row and prof_row["specialty"]:
            specialty = prof_row["specialty"].strip()
        else:
            specialty = (row["doctor_specialty"] or "").strip() if row else ""
    elif portal == "patient":
        specialty = "Patient portal"

    blood_group = ""
    profile_photo = ""
    health_score = 75
    short_status = "Healthy"
    
    if portal == "patient":
        try:
            p_row = db.execute("SELECT full_name, blood_group, profile_photo FROM patient_profiles WHERE user_id = ?", (user_id,)).fetchone()
            if p_row:
                if p_row["full_name"]:
                    display_name = p_row["full_name"].strip()
                blood_group = p_row["blood_group"] or ""
                profile_photo = p_row["profile_photo"] or ""
            
            if not blood_group and row and row["blood_group"]:
                try:
                    blood_group = decrypt_text(row["blood_group"]) or ""
                except Exception:
                    pass
            
            # Fetch health score
            hs_data = _get_health_score_data(user_id)
            health_score = hs_data.get("value", 75)
            
            # Check status
            appt = db.execute("SELECT 1 FROM appointments WHERE user_id = ? AND status = 'scheduled' LIMIT 1", (user_id,)).fetchone()
            if appt:
                short_status = "Follow-up Due"
            elif health_score >= 80:
                short_status = "Healthy"
            else:
                short_status = "Under Observation"
        except Exception:
            pass

    clean = display_name.replace("Dr.", "").replace("dr.", "").strip()
    parts = clean.split()
    if len(parts) >= 2:
        initials = (parts[0][0] + parts[-1][0]).upper()
    elif clean:
        initials = clean[:2].upper()
    else:
        initials = username[:2].upper()
    patient_id_str = f"#{user_id}" if portal == "patient" else ""

    return {
        "accent": accent,
        "accent_rgb": _hex_to_rgb_css(accent),
        "mode": mode,
        "portal_role": portal,
        "display_name": display_name,
        "specialty": specialty,
        "initials": initials,
        "username": username,
        "patient_id_str": patient_id_str,
        "portal_label": "Doctor Portal" if portal == "doctor" else "Patient Portal",
        "blood_group": blood_group,
        "profile_photo": profile_photo,
        "health_score": health_score,
        "short_status": short_status,
    }


def _redirect_home():
    if _portal_role(current_user.id) == "doctor":
        return redirect(url_for("core.doctor_dashboard"))
    return redirect(url_for("core.dashboard"))


def _require_doctor():
    if _portal_role(current_user.id) != "doctor":
        flash("That area is for doctors only.")
        return redirect(url_for("core.index"))
    return None


def _require_patient():
    if _portal_role(current_user.id) != "patient":
        flash("That area is for patients only.")
        return redirect(url_for("core.index"))
    return None


def _patient_greeting_name(full_name: str, username: str) -> str:
    n = (full_name or "").strip()
    if n:
        return n.split()[0]
    u = (username or "").strip()
    return u or "there"


def _fmt_appt_display(at_raw: str) -> str:
    s = (at_raw or "").strip().replace("Z", "")
    if not s:
        return "—"
    try:
        if "T" in s:
            dt = datetime.strptime(s[:16], "%Y-%m-%dT%H:%M")
        else:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
        return dt.strftime("%b %d, %Y · %I:%M %p") if "T" in (at_raw or "") else dt.strftime("%b %d, %Y")
    except ValueError:
        return at_raw or "—"


def _doctor_initials(name: str) -> str:
    parts = (name or "").strip().split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    n = (name or "").strip()
    if len(n) >= 2:
        return n[:2].upper()
    return "?"


def _today_bounds() -> tuple[int, int]:
    now = datetime.now()
    start = int(datetime(now.year, now.month, now.day).timestamp())
    return start, start + 86400


def _fmt_visit_time(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%I:%M %p")


def _seed_demo_appointments(db, doctor_id: int) -> None:
    c = db.execute(
        "SELECT COUNT(*) AS c FROM doctor_appointments WHERE doctor_user_id = ?",
        (doctor_id,),
    ).fetchone()["c"]
    if c > 0:
        return
    now = datetime.now()
    base = int(datetime(now.year, now.month, now.day).timestamp())
    demos = [
        ("Sarah Johnson", "Routine Checkup", base + 10 * 3600, "waiting", 15),
        ("Michael Chen", "Follow-up", base + 11 * 3600 + 30 * 60, "in_progress", 30),
        ("Emily Davis", "Lab Review", base + 14 * 3600, "scheduled", 20),
        ("Robert Wilson", "Diabetic checkup", base - 86400 * 2, "completed", 45),
        ("Jessica Taylor", "General consultations", base - 86400 * 3, "completed", 15),
        ("James Smith", "Cardio follow-up", base - 86400 * 8, "completed", 60),
        ("William Jones", "Routine review", base - 86400 * 15, "completed", 30),
    ]
    ts = now_ts()
    for name, reason, vts, status, dur in demos:
        db.execute(
            """
            INSERT INTO doctor_appointments
            (doctor_user_id, patient_name, reason, visit_ts, status, duration_minutes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (doctor_id, name, reason, vts, status, dur, ts),
        )


def _seed_demo_patients(db, doctor_id: int) -> None:
    c = db.execute("SELECT COUNT(*) AS c FROM doctor_patients WHERE doctor_user_id = ?", (doctor_id,)).fetchone()["c"]
    if c > 0:
        return
    patients = [
        ("Sarah Johnson", "555-0101", "Frequent allergy updates requested.", now_ts() - 86400 * 5),
        ("Michael Chen", "555-0102", "Hypertension patient, monitors BP.", now_ts() - 86400 * 10),
        ("Emily Davis", "555-0103", "Follow-up for blood report analysis.", now_ts() - 86400 * 12),
        ("Robert Wilson", "555-0104", "Diabetic patient, checking insulin dose.", now_ts() - 86400 * 15),
        ("Jessica Taylor", "555-0105", "Routine checkup patient.", now_ts() - 86400 * 18),
        ("David Miller", "555-0106", "Requires respiratory therapy advice.", now_ts() - 86400 * 20),
        ("James Smith", "555-0107", "Referred for cardiology review.", now_ts() - 86400 * 25),
        ("Linda Brown", "555-0108", "Follow-up on surgical recovery.", now_ts() - 86400 * 30),
        ("William Jones", "555-0109", "General health assessment.", now_ts() - 86400 * 35),
        ("Elizabeth Garcia", "555-0110", "Pediatric consultation follow-up.", now_ts() - 86400 * 40)
    ]
    for name, phone, notes, created in patients:
        db.execute(
            """
            INSERT INTO doctor_patients (doctor_user_id, full_name, phone, notes, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (doctor_id, name, phone, notes, created)
        )


def _seed_demo_payments(db, doctor_id: int) -> None:
    c = db.execute("SELECT COUNT(*) AS c FROM doctor_payments WHERE doctor_user_id = ?", (doctor_id,)).fetchone()["c"]
    if c > 0:
        return
    
    now = datetime.now()
    base = int(datetime(now.year, now.month, now.day).timestamp())
    ts = int(time.time())
    
    prof = db.execute("SELECT consultation_fee FROM doctor_profiles WHERE user_id = ?", (doctor_id,)).fetchone()
    fee = prof["consultation_fee"] if prof and prof["consultation_fee"] else 150.0
    
    appts = db.execute("SELECT id, patient_name, visit_ts FROM doctor_appointments WHERE doctor_user_id = ?", (doctor_id,)).fetchall()
    
    demos = [
        ("Sarah Johnson", 0, fee, "Paid", "UPI", "TXN90234"),
        ("Michael Chen", -7200, fee, "Paid", "Card", "TXN90235"),
        ("Emily Davis", -14400, fee, "Pending", "Cash", None),
        ("Robert Wilson", -86400 * 2, fee, "Paid", "Net Banking", "TXN90212"),
        ("Jessica Taylor", -86400 * 3, fee, "Paid", "UPI", "TXN90213"),
        ("David Miller", -86400 * 4, fee, "Failed", "Card", "TXN90214"),
        ("James Smith", -86400 * 8, fee, "Paid", "Cash", None),
        ("Linda Brown", -86400 * 12, fee, "Refunded", "Card", "TXN90199"),
        ("William Jones", -86400 * 15, fee, "Paid", "UPI", "TXN90188"),
        ("Elizabeth Garcia", -86400 * 32, fee, "Paid", "Card", "TXN90001"),
        ("Richard Martinez", -86400 * 45, fee, "Paid", "Net Banking", "TXN90002"),
        ("Barbara Davis", -86400 * 60, fee, "Paid", "UPI", "TXN90003"),
        ("Joseph Rodriguez", -86400 * 75, fee, "Paid", "Cash", None),
        ("Susan Wilson", -86400 * 90, fee, "Paid", "Card", "TXN90004"),
        ("Thomas Anderson", -86400 * 105, fee, "Refunded", "UPI", "TXN90005")
    ]
    
    for i, (name, time_offset, p_fee, status, method, txn) in enumerate(demos):
        vts = base + time_offset
        appt_id = appts[i % len(appts)]["id"] if appts else None
        db.execute(
            """
            INSERT INTO doctor_payments
            (doctor_user_id, patient_name, appointment_id, visit_ts, consultation_fee, status, payment_method, transaction_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (doctor_id, name, appt_id, vts, p_fee, status, method, txn, ts)
        )


def _doctor_notifications(db, doctor_id: int) -> list[str]:
    out: list[str] = []
    for row in db.execute(
        """
        SELECT patient_name, medicine_name, created_at
        FROM doctor_prescriptions
        WHERE doctor_user_id = ?
        ORDER BY created_at DESC
        LIMIT 2
        """,
        (doctor_id,),
    ).fetchall():
        out.append(f"Prescription recorded: {row['medicine_name']} for {row['patient_name']}")
    for row in db.execute(
        """
        SELECT full_name, created_at
        FROM doctor_patients
        WHERE doctor_user_id = ?
        ORDER BY created_at DESC
        LIMIT 2
        """,
        (doctor_id,),
    ).fetchall():
        out.append(f"New patient added: {row['full_name']}")
    for row in db.execute(
        """
        SELECT patient_name, chief_complaint, created_at
        FROM doctor_consultations
        WHERE doctor_user_id = ?
        ORDER BY created_at DESC
        LIMIT 2
        """,
        (doctor_id,),
    ).fetchall():
        cc = (row["chief_complaint"] or "").strip()
        out.append(f"Consultation: {row['patient_name']}" + (f" — {cc}" if cc else ""))
    if not out:
        return [
            "Lab results integration coming soon.",
            "Connect your clinic calendar in Settings.",
            "Use Scan patient emergency QR for urgent cases.",
        ]
    return out[:8]


def _analytics_payload(db, doctor_id: int) -> dict:
    """Enhanced analytics payload for doctor portal charts."""
    uid = int(doctor_id)
    now = datetime.now()
    months: list[str] = []
    y, mo = now.year, now.month
    for _ in range(6):
        months.insert(0, f"{y:04d}-{mo:02d}")
        mo -= 1
        if mo == 0:
            mo = 12
            y -= 1

    monthly_labels = [datetime(int(m[:4]), int(m[5:7]), 1).strftime("%b %y") for m in months]

    # 1. Revenue Chart (past 6 months)
    revenue_values = []
    for m in months:
        val = db.execute(
            """
            SELECT SUM(consultation_fee) AS s FROM doctor_payments
            WHERE doctor_user_id = ? AND status = 'Paid'
              AND strftime('%Y-%m', datetime(visit_ts, 'unixepoch')) = ?
            """,
            (uid, m),
        ).fetchone()["s"] or 0.0
        revenue_values.append(float(val))

    # 2. Monthly Patients Chart (growth)
    monthly_patients_values = []
    for m in months:
        val = db.execute(
            """
            SELECT COUNT(*) FROM doctor_patients
            WHERE doctor_user_id = ?
              AND strftime('%Y-%m', datetime(created_at, 'unixepoch')) = ?
            """,
            (uid, m),
        ).fetchone()[0]
        monthly_patients_values.append(int(val))

    # 3. Appointment Trends
    appt_status_list = ["scheduled", "waiting", "in_progress", "completed", "cancelled"]
    appt_labels = ["Scheduled", "Waiting", "In Progress", "Completed", "Cancelled"]
    appt_values = []
    for st in appt_status_list:
        val = db.execute(
            "SELECT COUNT(*) FROM doctor_appointments WHERE doctor_user_id = ? AND status = ?",
            (uid, st)
        ).fetchone()[0]
        appt_values.append(int(val))

    # 4. Consultation Duration Chart
    duration_labels = ["<15 mins", "15-30 mins", "30-45 mins", "45-60 mins", ">60 mins"]
    dur_counts = [0, 0, 0, 0, 0]
    for row in db.execute(
        "SELECT duration_minutes FROM doctor_appointments WHERE doctor_user_id = ? AND status = 'completed'",
        (uid,)
    ).fetchall():
        mins = row["duration_minutes"] or 30
        if mins < 15:
            dur_counts[0] += 1
        elif mins <= 30:
            dur_counts[1] += 1
        elif mins <= 45:
            dur_counts[2] += 1
        elif mins <= 60:
            dur_counts[3] += 1
        else:
            dur_counts[4] += 1
    duration_values = dur_counts

    # 5. Top Specializations / Symptoms
    buckets = {"Hypertension": 0, "Diabetes": 0, "Respiratory": 0, "Other": 0}
    for row in db.execute(
        "SELECT chief_complaint FROM doctor_consultations WHERE doctor_user_id = ?",
        (uid,),
    ).fetchall():
        t = (row["chief_complaint"] or "").lower()
        if any(k in t for k in ("bp", "pressure", "hypertension", "htn")):
            buckets["Hypertension"] += 1
        elif any(k in t for k in ("diabetes", "glucose", "sugar", "hba1c")):
            buckets["Diabetes"] += 1
        elif any(k in t for k in ("cough", "sob", "breath", "lung", "asthma")):
            buckets["Respiratory"] += 1
        elif t.strip():
            buckets["Other"] += 1
    if sum(buckets.values()) == 0:
        buckets = {"Hypertension": 12, "Diabetes": 8, "Respiratory": 6, "Other": 10}
    disease_labels = list(buckets.keys())
    disease_values = [buckets[k] for k in disease_labels]

    # 6. Peak Consultation Hours
    hour_list = [9, 10, 11, 12, 13, 14, 15, 16, 17]
    peak_labels = ["9 AM", "10 AM", "11 AM", "12 PM", "1 PM", "2 PM", "3 PM", "4 PM", "5 PM"]
    peak_values = [0] * len(hour_list)
    for row in db.execute(
        "SELECT visit_ts FROM doctor_appointments WHERE doctor_user_id = ?",
        (uid,)
    ).fetchall():
        dt = datetime.fromtimestamp(int(row["visit_ts"]))
        h = dt.hour
        if h in hour_list:
            idx = hour_list.index(h)
            peak_values[idx] += 1
    if sum(peak_values) == 0:
        peak_values = [3, 5, 4, 1, 2, 6, 4, 3, 1]

    # 7. Patient Retention Rate
    total_distinct = db.execute("SELECT COUNT(DISTINCT patient_name) FROM doctor_appointments WHERE doctor_user_id = ?", (uid,)).fetchone()[0] or 0
    returning = db.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT patient_name FROM doctor_appointments
            WHERE doctor_user_id = ?
            GROUP BY patient_name HAVING COUNT(*) > 1
        )
        """,
        (uid,)
    ).fetchone()[0] or 0
    retention_rate = round((returning / total_distinct * 100.0), 1) if total_distinct > 0 else 0.0

    return {
        "total_patients": db.execute("SELECT COUNT(*) FROM doctor_patients WHERE doctor_user_id = ?", (uid,)).fetchone()[0],
        "total_consults": db.execute("SELECT COUNT(*) FROM doctor_appointments WHERE doctor_user_id = ? AND status = 'completed'", (uid,)).fetchone()[0],
        "monthly_labels": monthly_labels,
        "revenue_values": revenue_values,
        "monthly_patients_values": monthly_patients_values,
        "appt_labels": appt_labels,
        "appt_values": appt_values,
        "duration_labels": duration_labels,
        "duration_values": duration_values,
        "disease_labels": disease_labels,
        "disease_values": disease_values,
        "peak_labels": peak_labels,
        "peak_values": peak_values,
        "retention_rate": retention_rate
    }


@core_bp.errorhandler(413)
def too_large(_e):
    flash("File too large. Max size is 8MB.")
    return _redirect_home()


@core_bp.get("/")
def index():
    if current_user.is_authenticated:
        db = get_db()
        u = db.execute(
            "SELECT onboarding_done, portal_role FROM users WHERE id = ?",
            (current_user.id,),
        ).fetchone()
        if not u:
            return redirect(url_for("auth.logout"))
        if (u["portal_role"] or "patient").strip().lower() == "doctor":
            return redirect(url_for("core.doctor_dashboard"))
        return redirect(url_for("core.dashboard"))
    return render_template("index.html")


def _get_health_score_data(uid: int):
    db = get_db()
    # Fetch profile details
    prof = db.execute("SELECT * FROM patient_profiles WHERE user_id = ?", (uid,)).fetchone()
    prof = dict(prof) if prof else {}
    
    # Fetch latest vitals
    vit = db.execute(
        """
        SELECT bp_systolic, bp_diastolic, sugar, heart_rate, weight, spo2, temperature, bmi, water_intake, steps, sleep_hours
        FROM vitals_logs WHERE user_id = ? ORDER BY logged_at DESC, id DESC LIMIT 1
        """,
        (uid,),
    ).fetchone()
    vit = dict(vit) if vit else {}
    
    # Calculate factors
    bp_sys = vit.get("bp_systolic")
    bp_dia = vit.get("bp_diastolic")
    sugar = vit.get("sugar")
    heart_rate = vit.get("heart_rate")
    
    height = prof.get("height", 0.0)
    weight = vit.get("weight") or prof.get("weight", 0.0)
    
    sleep = vit.get("sleep_hours") if vit.get("sleep_hours") is not None else prof.get("sleep_duration", 0.0)
    exercise = prof.get("exercise_frequency", "Sedentary")
    water = vit.get("water_intake") if vit.get("water_intake") is not None else prof.get("water_intake", 0.0)
    steps = vit.get("steps")
    spo2 = vit.get("spo2")
    temp = vit.get("temperature")
    
    # Adherence: from medication reminders completed today vs total today
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
    today_end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999).strftime("%Y-%m-%dT%H:%M")
    
    reminders_today = db.execute(
        """
        SELECT COUNT(*) as c, SUM(is_done) as done FROM reminders
        WHERE user_id = ? AND reminder_type = 'medication' AND remind_at >= ? AND remind_at <= ?
        """,
        (uid, today_start, today_end),
    ).fetchone()
    
    total_rem = reminders_today["c"] or 0
    done_rem = reminders_today["done"] or 0
    adherence = 100 if total_rem == 0 else int((done_rem / total_rem) * 100)
    
    # Compute Health Score
    score = 65  # Base score
    
    # BP Score impact
    bp_status = "Not Logged"
    bp_val = "N/A"
    if bp_sys and bp_dia:
        bp_val = f"{bp_sys}/{bp_dia}"
        if bp_sys < 120 and bp_dia < 80:
            bp_status = "Optimal"
            score += 5
        elif bp_sys < 130 and bp_dia < 85:
            bp_status = "Normal"
            score += 4
        elif bp_sys < 140 or bp_dia < 90:
            bp_status = "Prehypertension"
            score += 1
        else:
            bp_status = "High"
            score -= 5
            
    # Sugar Score impact
    sugar_status = "Not Logged"
    sugar_val = "N/A"
    if sugar:
        sugar_val = f"{sugar} mg/dL"
        if sugar < 100:
            sugar_status = "Normal (Fasting)"
            score += 5
        elif sugar < 140:
            sugar_status = "Normal (Postprandial)"
            score += 4
        elif sugar < 200:
            sugar_status = "Prediabetes"
            score += 1
        else:
            sugar_status = "High"
            score -= 5
            
    # BMI Score impact
    bmi_status = "N/A"
    bmi_val = "N/A"
    latest_bmi = vit.get("bmi")
    if latest_bmi:
        bmi_val = str(latest_bmi)
        if 18.5 <= latest_bmi < 25:
            bmi_status = "Normal"
            score += 5
        elif 25 <= latest_bmi < 30:
            bmi_status = "Overweight"
            score += 2
        else:
            bmi_status = "Obese"
            score -= 3
    elif height > 0 and weight > 0:
        h_m = height / 100.0
        bmi = round(weight / (h_m * h_m), 1)
        bmi_val = str(bmi)
        if 18.5 <= bmi < 25:
            bmi_status = "Normal"
            score += 5
        elif 25 <= bmi < 30:
            bmi_status = "Overweight"
            score += 2
        else:
            bmi_status = "Obese"
            score -= 3
            
    # Heart Rate impact
    hr_status = "Not Logged"
    hr_val = "N/A"
    if heart_rate:
        hr_val = f"{heart_rate} bpm"
        if 60 <= heart_rate <= 100:
            hr_status = "Normal"
            score += 5
        else:
            hr_status = "Abnormal"
            score -= 3
            
    # SpO2 impact
    spo2_status = "Not Logged"
    spo2_val = "N/A"
    if spo2:
        spo2_val = f"{spo2}%"
        if spo2 >= 95:
            spo2_status = "Normal"
            score += 5
        else:
            spo2_status = "Low"
            score -= 5

    # Temperature impact
    temp_status = "Not Logged"
    temp_val = "N/A"
    if temp:
        temp_val = f"{temp} °C"
        if 36.1 <= temp <= 37.2:
            temp_status = "Normal"
            score += 5
        else:
            temp_status = "Abnormal"
            score -= 3

    # Steps impact
    steps_status = "Not Logged"
    steps_val = "N/A"
    if steps:
        steps_val = f"{steps} steps"
        if steps >= 8000:
            steps_status = "Active"
            score += 5
        elif steps >= 5000:
            steps_status = "Moderately Active"
            score += 3
        else:
            steps_status = "Sedentary"
            score += 1

    # Sleep impact
    sleep_status = "Optimal" if sleep >= 7 else "Low"
    if sleep >= 7:
        score += 5
    else:
        score += 2
        
    # Exercise impact
    ex_status = "Optimal" if exercise in ["3-4 times/week", "Daily"] else ("Moderate" if exercise == "1-2 times/week" else "Sedentary")
    if exercise in ["3-4 times/week", "Daily"]:
        score += 5
    elif exercise == "1-2 times/week":
        score += 3
        
    # Water impact
    water_status = "Optimal" if water >= 2.0 else ("Moderate" if water >= 1.0 else "Low")
    if water >= 2.0:
        score += 5
    elif water >= 1.0:
        score += 3
        
    # Adherence impact
    if adherence >= 90:
        score += 5
    elif adherence >= 70:
        score += 3
    else:
        score -= 2
        
    score = min(100, max(10, score))
    
    # Categories
    if score >= 85:
        category = "Excellent"
        label = "Excellent health"
    elif score >= 70:
        category = "Good"
        label = "Good progress"
    elif score >= 50:
        category = "Fair"
        label = "Fair health"
    else:
        category = "Needs Attention"
        label = "Needs attention"
        
    factors = [
        {"name": "Blood Pressure", "value": bp_val, "status": bp_status, "ok": bp_status in ["Optimal", "Normal"]},
        {"name": "Blood Sugar", "value": sugar_val, "status": sugar_status, "ok": "Normal" in sugar_status},
        {"name": "BMI", "value": bmi_val, "status": bmi_status, "ok": bmi_status == "Normal"},
        {"name": "Heart Rate", "value": hr_val, "status": hr_status, "ok": hr_status == "Normal"},
        {"name": "Oxygen Saturation (SpO₂)", "value": spo2_val, "status": spo2_status, "ok": spo2_status == "Normal"},
        {"name": "Body Temperature", "value": temp_val, "status": temp_status, "ok": temp_status == "Normal"},
        {"name": "Sleep", "value": f"{sleep} hrs/day", "status": sleep_status, "ok": sleep_status == "Optimal"},
        {"name": "Water Intake", "value": f"{water} L/day", "status": water_status, "ok": water_status == "Optimal"},
        {"name": "Daily Steps", "value": steps_val, "status": steps_status, "ok": steps_status in ["Active", "Moderately Active"]},
        {"name": "Medication Adherence", "value": f"{adherence}%", "status": "Good" if adherence >= 80 else "Poor", "ok": adherence >= 80},
    ]
    
    suggestions = []
    improvements = []
    attention = []
    
    if bp_status == "High":
        suggestions.append("Your blood pressure is elevated. Reduce sodium in your diet and engage in moderate exercise.")
        attention.append("Blood Pressure is high")
    elif bp_status in ["Optimal", "Normal"]:
        improvements.append("Maintaining blood pressure in a healthy range.")
        
    if sugar_status == "High":
        suggestions.append("Your blood sugar level is high. Limit simple carbohydrates and consult your physician.")
        attention.append("Blood Sugar is high")
    elif "Normal" in sugar_status:
        improvements.append("Healthy blood sugar control.")
        
    if bmi_status in ["Overweight", "Obese"]:
        suggestions.append("Your BMI is outside the normal range. Focus on nutrient-rich foods and daily activity.")
        attention.append("Weight/BMI exceeds normal range")
    elif bmi_status == "Normal":
        improvements.append("BMI is in the ideal range.")
        
    if sleep < 7:
        suggestions.append("Aim for 7-9 hours of restful sleep daily to promote muscle recovery and cognitive health.")
        attention.append("Averaging less than 7 hours of sleep")
    else:
        improvements.append("Sufficient daily sleep duration.")
        
    if water < 2.0:
        suggestions.append("Increase your water intake to 2+ liters daily for proper cellular hydration.")
        attention.append("Hydration level is low")
    else:
        improvements.append("Excellent hydration habit.")
        
    if exercise == "Sedentary":
        suggestions.append("Incorporate at least 150 minutes of moderate aerobic exercise weekly.")
        attention.append("Sedentary lifestyle")
        
    if adherence < 80:
        suggestions.append("Set alarms or use daily reminders to improve your medication consistency.")
        attention.append("Low medication adherence rate")
        
    if not suggestions:
        suggestions.append("Keep doing what you are doing! Maintain your current healthy routine.")
        
    history = [
        {"date": "1 Month Ago", "score": max(30, score - 5)},
        {"date": "2 Weeks Ago", "score": max(30, score - 2)},
        {"date": "Today", "score": score}
    ]
    
    return {
        "value": score,
        "category": category,
        "label": label,
        "factors": factors,
        "suggestions": suggestions,
        "improvements": improvements,
        "attention": attention,
        "history": history
    }


def _generate_ai_health_analysis(uid: int):
    db = get_db()
    logs = db.execute(
        """
        SELECT bp_systolic, bp_diastolic, sugar, heart_rate, spo2, temperature, water_intake, sleep_hours, logged_at
        FROM vitals_logs WHERE user_id = ? ORDER BY logged_at DESC LIMIT 10
        """,
        (uid,)
    ).fetchall()
    
    insights = []
    recommendations = []
    alerts = []
    
    now = datetime.now()
    if logs:
        latest = logs[0]
        try:
            latest_dt = datetime.strptime(latest["logged_at"][:16], "%Y-%m-%dT%H:%M")
        except ValueError:
            try:
                latest_dt = datetime.strptime(latest["logged_at"][:16], "%Y-%m-%d %H:%M")
            except ValueError:
                latest_dt = now
        
        days_since = (now - latest_dt).days
        if days_since >= 2:
            insights.append("You have missed logging your vitals for two consecutive days.")
            recommendations.append("Resume logging your vitals daily to keep tracking your health.")
    else:
        insights.append("You haven't logged any daily health readings yet.")
        recommendations.append("Log today's vitals to get personalized health index insights.")
        
    if logs:
        latest = logs[0]
        bp_sys = latest["bp_systolic"]
        bp_dia = latest["bp_diastolic"]
        if bp_sys and bp_dia:
            if bp_sys >= 140 or bp_dia >= 90:
                alerts.append({
                    "title": "High Blood Pressure Detected",
                    "desc": f"Your latest blood pressure is {bp_sys}/{bp_dia} mmHg. Hypertensive range.",
                    "level": "high"
                })
                recommendations.append("Monitor blood pressure daily and restrict salt intake.")
            else:
                insights.append(f"Your latest blood pressure is optimal ({bp_sys}/{bp_dia} mmHg).")
                
            if len(logs) >= 3:
                recent_sys_count = sum(1 for r in logs[:3] if r["bp_systolic"])
                past_sys_count = sum(1 for r in logs[3:6] if r["bp_systolic"])
                if recent_sys_count > 0 and past_sys_count > 0:
                    recent_sys_avg = sum(r["bp_systolic"] for r in logs[:3] if r["bp_systolic"]) / recent_sys_count
                    past_sys_avg = sum(r["bp_systolic"] for r in logs[3:6] if r["bp_systolic"]) / past_sys_count
                    if recent_sys_avg - past_sys_avg > 5:
                        insights.append("Your blood pressure has increased over the past few days.")
                        recommendations.append("Avoid caffeine, manage stress, and track your BP twice daily.")
        
        sugar = latest["sugar"]
        if sugar:
            if sugar >= 200:
                alerts.append({
                    "title": "Extremely High Blood Sugar",
                    "desc": f"Your blood sugar level is {sugar} mg/dL. Hyperglycemia range.",
                    "level": "high"
                })
                recommendations.append("Consider repeating your blood sugar test and consult your doctor.")
            elif 70 <= sugar <= 140:
                insights.append("Your blood sugar is within the normal range.")
            else:
                insights.append(f"Your latest blood sugar is {sugar} mg/dL.")
        
        hr = latest["heart_rate"]
        if hr:
            if hr > 100 or hr < 50:
                alerts.append({
                    "title": "Abnormal Heart Rate",
                    "desc": f"Your resting heart rate of {hr} bpm is abnormal.",
                    "level": "high"
                })
                recommendations.append("Monitor your heart rate during rest and limit stimulants.")
            elif 60 <= hr <= 90:
                insights.append("Your resting heart rate has improved this week.")
        
        spo2 = latest["spo2"]
        if spo2:
            if spo2 < 95:
                alerts.append({
                    "title": "Low Oxygen Saturation",
                    "desc": f"Your oxygen saturation is low at {spo2}% (Normal: 95-100%).",
                    "level": "high"
                })
                recommendations.append("Practice breathing exercises and contact your doctor if shortness of breath occurs.")
            else:
                insights.append(f"Your oxygen saturation is healthy at {spo2}%.")
                
        water = latest["water_intake"]
        if water and water < 2.0:
            recommendations.append("Drink more water (target 2-3 Liters daily).")
        sleep = latest["sleep_hours"]
        if sleep and sleep < 7.0:
            recommendations.append("Improve sleep (aim for 7-9 hours of restful sleep).")
            
    if not recommendations:
        recommendations.append("Continue your prescribed medication cycle on schedule.")
        recommendations.append("Maintain moderate daily activity and balanced diet.")
    if len(recommendations) < 3:
        recommendations.append("Book a follow-up consultation in case of any new symptoms.")
        
    return {
        "insights": insights,
        "recommendations": recommendations,
        "alerts": alerts
    }


@core_bp.get("/dashboard")
@login_required
def dashboard():
    redir = _require_patient()
    if redir:
        return redir
    search = (request.args.get("q") or "").strip()
    db = get_db()
    user_row = db.execute(
        "SELECT id, username, full_name, blood_group, allergies, medications, conditions FROM users WHERE id = ?",
        (current_user.id,),
    ).fetchone()
    if search:
        file_rows = db.execute(
            """
            SELECT id, filename, uploaded_at, category, category_confidence, doc_category, doc_source
            FROM files
            WHERE user_id = ? AND filename LIKE ?
            ORDER BY uploaded_at DESC
            """,
            (current_user.id, f"%{search}%"),
        ).fetchall()
    else:
        file_rows = db.execute(
            """
            SELECT id, filename, uploaded_at, category, category_confidence, doc_category, doc_source
            FROM files
            WHERE user_id = ?
            ORDER BY uploaded_at DESC
            """,
            (current_user.id,),
        ).fetchall()
    files = []
    for f in file_rows:
        d = dict(f)
        d["uploaded_label"] = datetime.fromtimestamp(int(f["uploaded_at"])).strftime("%b %d, %Y")
        files.append(d)
    profile = {
        "username": user_row["username"],
        "full_name": decrypt_text(user_row["full_name"]),
        "blood_group": decrypt_text(user_row["blood_group"]),
        "allergies": decrypt_text(user_row["allergies"]),
        "medications": decrypt_text(user_row["medications"]),
        "conditions": decrypt_text(user_row["conditions"]),
        
        # Default fields for patient profile attributes
        "age": 0,
        "gender": "",
        "dob": "",
        "height": 0.0,
        "weight": 0.0,
        "phone": "",
        "email": "",
        "address": "",
        "emergency_contact_name": "",
        "emergency_contact_number": "",
        "existing_diseases": "",
        "current_medications": "",
        "previous_surgeries": "",
        "family_medical_history": "",
        "smoking_status": "Non-smoker",
        "alcohol_consumption": "Non-drinker",
        "exercise_frequency": "1-2 times/week",
        "sleep_duration": 0.0,
        "diet_preference": "Non-Vegetarian",
        "water_intake": 0.0,
        "occupation": "",
        "preferred_language": "English",
        "preferred_consultation_mode": "In-person"
    }
    
    prof_row = db.execute("SELECT * FROM patient_profiles WHERE user_id = ?", (current_user.id,)).fetchone()
    if prof_row:
        profile.update(dict(prof_row))

    medical_summary = generate_medical_summary(user_row)
    theme = _portal_theme(current_user.id, current_user.username)
    uid = current_user.id

    vit = db.execute(
        """
        SELECT bp_systolic, bp_diastolic, heart_rate, weight
        FROM vitals_logs WHERE user_id = ? ORDER BY id DESC LIMIT 1
        """,
        (uid,),
    ).fetchone()
    if vit and vit["bp_systolic"] and vit["bp_diastolic"]:
        bp_display = f"{vit['bp_systolic']}/{vit['bp_diastolic']} mmHg"
    else:
        bp_display = "No data"
    hr_display = int(vit["heart_rate"]) if vit and vit["heart_rate"] is not None else "No data"
    if vit and vit["weight"] is not None:
        weight_lbs = f"{round(float(vit['weight']) * 2.20462, 1):g} lbs"
    else:
        weight_lbs = "No data"
    temp_display = "No data"

    vitals_cards = [
        {"label": "Blood pressure", "value": bp_display, "status": "Normal" if bp_display != "No data" else "N/A", "tone": "pink"},
        {"label": "Heart rate", "value": f"{hr_display} bpm" if hr_display != "No data" else "No data", "status": "Normal" if hr_display != "No data" else "N/A", "tone": "teal"},
        {"label": "Temperature", "value": temp_display, "status": "N/A", "tone": "orange"},
        {"label": "Weight", "value": weight_lbs, "status": "Normal" if weight_lbs != "No data" else "N/A", "tone": "purple"},
    ]

    ap_rows = db.execute(
        """
        SELECT a.id, a.appointment_at, a.reason, a.status, d.name AS doctor_name, d.specialization
        FROM appointments a
        LEFT JOIN doctors d ON a.doctor_id = d.id
        WHERE a.user_id = ? AND COALESCE(a.status, '') != 'cancelled'
        ORDER BY a.appointment_at ASC
        """,
        (uid,),
    ).fetchall()
    now = datetime.now()
    upcoming_items = []
    for r in ap_rows:
        at = r["appointment_at"] or ""
        try:
            if "T" in at:
                apt_dt = datetime.strptime(at[:16], "%Y-%m-%dT%H:%M")
            elif " " in at:
                apt_dt = datetime.strptime(at[:16], "%Y-%m-%d %H:%M")
            else:
                apt_dt = datetime.strptime(at[:10], "%Y-%m-%d")
        except ValueError:
            continue
        if apt_dt < now - timedelta(hours=2):
            continue
            
        dn = (r["doctor_name"] or "").strip() or "Your provider"
        doc_display = dn if dn.lower().startswith("dr") else f"Dr. {dn}"
        reason = (r["reason"] or "").strip() or "Visit"
        low = reason.lower()
        badge = "Online Video" if ("video" in low or "tele" in low or "online" in low) else "In-person"
        
        # Countdown
        delta = apt_dt - now
        if delta.total_seconds() > 0:
            hours = int(delta.total_seconds() // 3600)
            minutes = int((delta.total_seconds() % 3600) // 60)
            if hours >= 24:
                days = hours // 24
                countdown = f"Starts in {days} day{'s' if days > 1 else ''}"
            elif hours > 0:
                countdown = f"Starts in {hours}h {minutes}m"
            else:
                countdown = f"Starts in {minutes}m"
        else:
            countdown = "Started/Happening now"
            
        upcoming_items.append(
            {
                "id": r["id"],
                "doctor_name": doc_display,
                "specialization": (r["specialization"] or "General practice").strip(),
                "reason": reason,
                "type": badge,
                "date_str": apt_dt.strftime("%b %d, %Y"),
                "time_str": apt_dt.strftime("%I:%M %p"),
                "status": r["status"] or "scheduled",
                "countdown": countdown,
                "raw_date": apt_dt.strftime("%Y-%m-%d"),
                "raw_time": apt_dt.strftime("%H:%M"),
                "when_fmt": _fmt_appt_display(at),
            }
        )

    rx_rows = db.execute(
        """
        SELECT medicine_name, dosage, frequency, end_date, start_date
        FROM prescriptions WHERE user_id = ? ORDER BY id DESC LIMIT 4
        """,
        (uid,),
    ).fetchall()
    rx_list = [
        {
            "name": row["medicine_name"],
            "dose": row["dosage"],
            "freq": row["frequency"],
            "refill": (row["end_date"] or row["start_date"] or "See pharmacy").strip(),
            "demo": False,
        }
        for row in rx_rows
    ]

    health_score_data = _get_health_score_data(uid)
    health_score = {
        "value": health_score_data["value"],
        "label": health_score_data["label"]
    }

    # Fetch latest vitals details for summary card
    latest_vit_row = db.execute(
        """
        SELECT bp_systolic, bp_diastolic, sugar, heart_rate, spo2, temperature, logged_at
        FROM vitals_logs WHERE user_id = ? ORDER BY logged_at DESC, id DESC LIMIT 1
        """,
        (uid,)
    ).fetchone()
    
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_vitals_recorded = False
    if latest_vit_row and latest_vit_row["logged_at"] and latest_vit_row["logged_at"].startswith(today_str):
        today_vitals_recorded = True
        
    latest_vitals = {
        "bp": f"{latest_vit_row['bp_systolic']}/{latest_vit_row['bp_diastolic']} mmHg" if (latest_vit_row and latest_vit_row["bp_systolic"] is not None and latest_vit_row["bp_diastolic"] is not None and today_vitals_recorded) else "Not logged today",
        "sugar": f"{latest_vit_row['sugar']} mg/dL" if (latest_vit_row and latest_vit_row["sugar"] is not None and today_vitals_recorded) else "Not logged today",
        "hr": f"{latest_vit_row['heart_rate']} bpm" if (latest_vit_row and latest_vit_row["heart_rate"] is not None and today_vitals_recorded) else "Not logged today",
        "spo2": f"{latest_vit_row['spo2']}%" if (latest_vit_row and latest_vit_row["spo2"] is not None and today_vitals_recorded) else "Not logged today",
        "temp": f"{latest_vit_row['temperature']} °C" if (latest_vit_row and latest_vit_row["temperature"] is not None and today_vitals_recorded) else "Not logged today",
        "updated": latest_vit_row['logged_at'].replace('T', ' ') if latest_vit_row and latest_vit_row["logged_at"] else "Never",
        "recorded_today": today_vitals_recorded
    }

    greeting_name = _patient_greeting_name(profile["full_name"], profile["username"])
    first_appt = upcoming_items[0] if upcoming_items else None

    # Fetch recent activity
    last_appt_row = db.execute(
        """
        SELECT a.appointment_at, d.name as doctor_name
        FROM appointments a
        LEFT JOIN doctors d ON a.doctor_id = d.id
        WHERE a.user_id = ?
        ORDER BY a.appointment_at DESC LIMIT 1
        """,
        (uid,)
    ).fetchone()
    last_appt = f"{_fmt_appt_display(last_appt_row['appointment_at'])} with {last_appt_row['doctor_name']}" if last_appt_row else "No past appointments"
    
    last_rx_row = db.execute(
        "SELECT medicine_name, created_at FROM prescriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (uid,)
    ).fetchone()
    last_rx = f"{last_rx_row['medicine_name']} (on {datetime.fromtimestamp(int(last_rx_row['created_at'])).strftime('%b %d, %Y')})" if last_rx_row else "No prescriptions"
    
    last_file_row = db.execute(
        "SELECT filename, uploaded_at FROM files WHERE user_id = ? ORDER BY uploaded_at DESC LIMIT 1",
        (uid,)
    ).fetchone()
    last_file = f"{last_file_row['filename']} (on {datetime.fromtimestamp(int(last_file_row['uploaded_at'])).strftime('%b %d, %Y')})" if last_file_row else "No uploaded reports"

    recent_activities = [
        {"icon": "📅", "title": "Last Appointment", "desc": last_appt},
        {"icon": "💊", "title": "Last Prescription", "desc": last_rx},
        {"icon": "📄", "title": "Last Uploaded Report", "desc": last_file}
    ]

    # Fetch upcoming tasks
    upcoming_tasks = []
    for appt in upcoming_items[:2]:
        upcoming_tasks.append({
            "type": "appointment",
            "title": f"Appointment with {appt['doctor_name']}",
            "time": appt['when_fmt'],
            "icon": "📅"
        })
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
    today_end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999).strftime("%Y-%m-%dT%H:%M")
    rem_rows = db.execute(
        """
        SELECT title, remind_at FROM reminders
        WHERE user_id = ? AND reminder_type = 'medication' AND is_done = 0 AND remind_at >= ? AND remind_at <= ?
        ORDER BY remind_at ASC LIMIT 2
        """,
        (uid, today_start, today_end)
    ).fetchall()
    for rem in rem_rows:
        try:
            dt = datetime.strptime(rem["remind_at"][:16], "%Y-%m-%dT%H:%M")
            time_str = dt.strftime("%I:%M %p")
        except ValueError:
            time_str = rem["remind_at"]
        upcoming_tasks.append({
            "type": "medication",
            "title": f"Take {rem['title']}",
            "time": time_str,
            "icon": "💊"
        })

    # Medical Documents dashboard statistics
    doc_stats_total = db.execute("SELECT COUNT(*) as c FROM files WHERE user_id = ?", (uid,)).fetchone()
    total_docs = doc_stats_total["c"] if doc_stats_total else 0
    
    seven_days_ago_ts = int((datetime.now() - timedelta(days=7)).timestamp())
    doc_stats_recent = db.execute("SELECT COUNT(*) as c FROM files WHERE user_id = ? AND uploaded_at >= ?", (uid, seven_days_ago_ts)).fetchone()
    recent_docs_count = doc_stats_recent["c"] if doc_stats_recent else 0
    
    doc_stats_last = db.execute("SELECT uploaded_at FROM files WHERE user_id = ? ORDER BY uploaded_at DESC LIMIT 1", (uid,)).fetchone()
    if doc_stats_last:
        last_upload_date = datetime.fromtimestamp(int(doc_stats_last["uploaded_at"])).strftime("%b %d, %Y")
    else:
        last_upload_date = "No uploads"

    ai_analysis = _generate_ai_health_analysis(uid)

    return render_template(
        "dashboard.html",
        theme=theme,
        user=profile,
        files=files,
        search=search,
        medical_summary=medical_summary,
        greeting_name=greeting_name,
        vitals_cards=vitals_cards,
        upcoming_items=upcoming_items,
        first_appt=first_appt,
        rx_list=rx_list,
        health_score=health_score,
        recent_activities=recent_activities,
        upcoming_tasks=upcoming_tasks,
        latest_vitals=latest_vitals,
        health_score_data=health_score_data,
        ai_analysis=ai_analysis,
        total_docs=total_docs,
        recent_docs_count=recent_docs_count,
        last_upload_date=last_upload_date
    )


def _calc_profile_completion(db, uid) -> int:
    row = db.execute("SELECT * FROM doctor_profiles WHERE user_id = ?", (uid,)).fetchone()
    if not row:
        return 0
    # list of fields to check
    fields = [
        "full_name", "dob", "gender", "phone", "email", "address",
        "specialty", "registration_number", "medical_council", 
        "years_experience", "highest_qualification", "hospital_clinic", 
        "consultation_fee", "working_days", "consultation_hours", "bio"
    ]
    filled = 0
    for f in fields:
        val = row[f]
        if val is not None:
            if isinstance(val, (int, float)) and val > 0:
                filled += 1
            elif isinstance(val, str) and val.strip() != "":
                filled += 1
    pct = int((filled / len(fields)) * 100)
    return min(100, max(0, pct))


@core_bp.get("/doctor")
@login_required
def doctor_dashboard():
    if _portal_role(current_user.id) != "doctor":
        return redirect(url_for("core.dashboard"))
    db = get_db()
    _seed_demo_appointments(db, int(current_user.id))
    _seed_demo_patients(db, int(current_user.id))
    _seed_demo_payments(db, int(current_user.id))
    db.commit()

    theme = _portal_theme(current_user.id, current_user.username)
    uid = current_user.id
    t0, t1 = _today_bounds()

    # Clinic Overview Counts
    patients_today_n = db.execute(
        "SELECT COUNT(*) AS c FROM doctor_appointments WHERE doctor_user_id = ? AND visit_ts >= ? AND visit_ts < ?",
        (uid, t0, t1),
    ).fetchone()["c"]
    completed_n = db.execute(
        "SELECT COUNT(*) FROM doctor_appointments WHERE doctor_user_id = ? AND visit_ts >= ? AND visit_ts < ? AND status = 'completed'",
        (uid, t0, t1)
    ).fetchone()[0] or 0
    waiting_n = db.execute(
        "SELECT COUNT(*) FROM doctor_appointments WHERE doctor_user_id = ? AND visit_ts >= ? AND visit_ts < ? AND status = 'waiting'",
        (uid, t0, t1),
    ).fetchone()[0] or 0
    upcoming_n = db.execute(
        "SELECT COUNT(*) FROM doctor_appointments WHERE doctor_user_id = ? AND visit_ts >= ? AND visit_ts < ? AND status = 'scheduled'",
        (uid, t0, t1)
    ).fetchone()[0] or 0
    cancelled_n = db.execute(
        "SELECT COUNT(*) FROM doctor_appointments WHERE doctor_user_id = ? AND visit_ts >= ? AND visit_ts < ? AND status = 'cancelled'",
        (uid, t0, t1)
    ).fetchone()[0] or 0
    emergency_n = db.execute(
        "SELECT COUNT(*) FROM emergency_access_logs WHERE doctor_user_id = ? AND accessed_at >= ?",
        (uid, t0)
    ).fetchone()[0] or 0

    active_n = db.execute(
        "SELECT COUNT(*) AS c FROM doctor_consultations WHERE doctor_user_id = ? AND status = 'in_progress'",
        (uid,),
    ).fetchone()["c"]
    active_n += db.execute(
        "SELECT COUNT(*) AS c FROM doctor_appointments WHERE doctor_user_id = ? AND visit_ts >= ? AND visit_ts < ? AND status = 'in_progress'",
        (uid, t0, t1),
    ).fetchone()["c"]

    total_patients = db.execute(
        "SELECT COUNT(*) AS c FROM doctor_patients WHERE doctor_user_id = ?",
        (uid,),
    ).fetchone()["c"]

    # Earnings & Financials
    today_earnings = db.execute(
        "SELECT SUM(consultation_fee) FROM doctor_payments WHERE doctor_user_id = ? AND status = 'Paid' AND visit_ts >= ?",
        (uid, t0)
    ).fetchone()[0] or 0.0
    
    month_start = int(datetime(datetime.now().year, datetime.now().month, 1).timestamp())
    monthly_earnings = db.execute(
        "SELECT SUM(consultation_fee) FROM doctor_payments WHERE doctor_user_id = ? AND status = 'Paid' AND visit_ts >= ?",
        (uid, month_start)
    ).fetchone()[0] or 0.0

    month_treated = db.execute(
        "SELECT COUNT(*) AS c FROM doctor_patients WHERE doctor_user_id = ? AND created_at >= ?",
        (uid, month_start),
    ).fetchone()["c"]

    # Patient Growth Rate
    last_month_start = int((datetime(datetime.now().year, datetime.now().month, 1) - timedelta(days=28)).replace(day=1).timestamp())
    prev_month_treated = db.execute(
        "SELECT COUNT(*) FROM doctor_patients WHERE doctor_user_id = ? AND created_at >= ? AND created_at < ?",
        (uid, last_month_start, month_start)
    ).fetchone()[0] or 0
    growth_pct = round(((month_treated - prev_month_treated) / max(prev_month_treated, 1)) * 100, 1) if prev_month_treated > 0 else (month_treated * 100.0)

    # Next Appointment Info
    next_appt_row = db.execute(
        """
        SELECT patient_name, reason, visit_ts FROM doctor_appointments
        WHERE doctor_user_id = ? AND visit_ts >= ? AND status = 'scheduled'
        ORDER BY visit_ts ASC LIMIT 1
        """,
        (uid, now_ts()),
    ).fetchone()
    next_appt = None
    if next_appt_row:
        next_appt = {
            "name": next_appt_row["patient_name"],
            "reason": next_appt_row["reason"] or "Routine Consultation",
            "time": _fmt_visit_time(int(next_appt_row["visit_ts"]))
        }

    kpis = [
        {"label": "Total Patients", "value": str(total_patients), "delta": "", "tone": "teal", "up": True},
        {"label": "Appointments Today", "value": str(patients_today_n), "delta": "", "tone": "blue", "up": True},
        {"label": "Waiting now", "value": str(waiting_n), "delta": "", "tone": "pink", "up": waiting_n > 0},
        {"label": "Active consults", "value": str(active_n), "delta": "", "tone": "red", "up": active_n > 0},
    ]

    q = (request.args.get("q") or "").strip()
    sql = """
        SELECT id, patient_name, reason, visit_ts, status
        FROM doctor_appointments
        WHERE doctor_user_id = ? AND visit_ts >= ? AND visit_ts < ?
    """
    params = [uid, t0, t1]
    if q:
        sql += " AND (patient_name LIKE ? OR reason LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like])
    sql += " ORDER BY visit_ts ASC"
    rows = db.execute(sql, tuple(params)).fetchall()
    appointments = []
    for row in rows:
        st = row["status"] or "scheduled"
        appointments.append(
            {
                "id": row["id"],
                "initials": _doctor_initials(row["patient_name"]),
                "name": row["patient_name"],
                "sub": (row["reason"] or "").strip() or "Visit",
                "time": _fmt_visit_time(int(row["visit_ts"])),
                "status": st if st in ("waiting", "in_progress", "scheduled", "completed", "cancelled") else "scheduled",
            }
        )

    notifications = _doctor_notifications(db, uid)
    profile_completion = _calc_profile_completion(db, uid)
    is_verified = 1 if profile_completion >= 80 else 0

    rev_row = db.execute("SELECT SUM(consultation_fee) AS s FROM doctor_payments WHERE doctor_user_id = ? AND status = 'Paid'", (int(uid),)).fetchone()
    total_revenue = rev_row["s"] if rev_row and rev_row["s"] else 0.0
    
    dur_row = db.execute("SELECT SUM(duration_minutes) AS s FROM doctor_appointments WHERE doctor_user_id = ? AND status = 'completed'", (int(uid),)).fetchone()
    consultation_hours = round((dur_row["s"] if dur_row and dur_row["s"] else 0.0) / 60.0, 1)

    # Activity log
    logs = db.execute(
        "SELECT description, created_at FROM doctor_activity_logs WHERE doctor_user_id = ? ORDER BY created_at DESC LIMIT 5",
        (uid,)
    ).fetchall()
    activity_log = []
    for l in logs:
        dt = datetime.fromtimestamp(int(l["created_at"])).strftime("%b %d, %Y %I:%M %p")
        activity_log.append({
            "description": l["description"],
            "timestamp": dt
        })

    # AI Practice Insights
    practice_insights = _generate_practice_insights(db, uid)

    return render_template(
        "doctor_dashboard.html",
        theme=theme,
        kpis=kpis,
        appointments=appointments,
        notifications=notifications,
        appt_search=q,
        appt_has_query=bool(q),
        month_treated=month_treated,
        profile_completion=profile_completion,
        is_verified=is_verified,
        total_revenue=total_revenue,
        consultation_hours=consultation_hours,
        activity_log=activity_log,
        
        # Redesign metrics
        patients_today_n=patients_today_n,
        completed_n=completed_n,
        waiting_n=waiting_n,
        upcoming_n=upcoming_n,
        cancelled_n=cancelled_n,
        emergency_n=emergency_n,
        today_earnings=today_earnings,
        monthly_earnings=monthly_earnings,
        growth_pct=growth_pct,
        next_appt=next_appt,
        practice_insights=practice_insights,
    )


@core_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    db = get_db()
    role = _portal_role(current_user.id)
    theme = _portal_theme(current_user.id, current_user.username)
    if request.method == "POST":
        accent = _normalize_accent(request.form.get("theme_accent") or "")
        mode = (request.form.get("theme_mode") or "light").strip().lower()
        if mode not in ("light", "dark"):
            mode = "light"
        if role == "doctor":
            specialty = (request.form.get("doctor_specialty") or "").strip()[:120]
            db.execute(
                "UPDATE users SET theme_accent = ?, theme_mode = ?, doctor_specialty = ? WHERE id = ?",
                (accent, mode, specialty, current_user.id),
            )
        else:
            db.execute(
                "UPDATE users SET theme_accent = ?, theme_mode = ? WHERE id = ?",
                (accent, mode, current_user.id),
            )
        db.commit()
        flash("Appearance saved.")
        return _redirect_home()

    if role == "doctor":
        row = db.execute(
            "SELECT doctor_specialty FROM users WHERE id = ?",
            (current_user.id,),
        ).fetchone()
        return render_template(
            "doctor_settings.html",
            theme=theme,
            doctor_specialty=(row["doctor_specialty"] or "") if row else "",
            portal_role=role,
        )
    return render_template("patient_settings.html", theme=theme, portal_role=role)


@core_bp.route("/doctor/profile", methods=["GET", "POST"])
@login_required
def doctor_profile():
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    
    # Try fetching from doctor_profiles
    row = db.execute("SELECT * FROM doctor_profiles WHERE user_id = ?", (current_user.id,)).fetchone()
    
    if not row:
        # Fallback to users table
        user_row = db.execute(
            "SELECT full_name, doctor_specialty, doctor_phone, doctor_clinic, doctor_bio FROM users WHERE id = ?",
            (current_user.id,)
        ).fetchone()
        if not user_row:
            flash("Account not found.")
            return redirect(url_for("auth.logout"))
        
        # Build a temporary dict matching doctor_profiles columns
        profile = {
            "full_name": decrypt_text(user_row["full_name"]),
            "profile_photo": "",
            "gender": "Female",
            "dob": "",
            "phone": decrypt_text(user_row["doctor_phone"]),
            "email": current_user.username + "@medvault.com",
            "address": "",
            "city": "",
            "state": "",
            "country": "",
            "specialty": (user_row["doctor_specialty"] or "").strip(),
            "sub_specialty": "",
            "registration_number": "",
            "medical_council": "",
            "years_experience": 0,
            "highest_qualification": "",
            "college_university": "",
            "hospital_clinic": decrypt_text(user_row["doctor_clinic"]),
            "position": "",
            "consultation_fee": 0.0,
            "languages_spoken": "",
            "working_days": "Monday,Tuesday,Wednesday,Thursday,Friday",
            "consultation_hours": "09:00 AM - 05:00 PM",
            "timezone": "IST",
            "online_consultation": 1,
            "offline_consultation": 1,
            "bio": decrypt_text(user_row["doctor_bio"]),
            "expertise": "",
            "certifications": "",
            "awards": ""
        }
    else:
        profile = dict(row)
        
    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        profile_photo = (request.form.get("profile_photo") or "").strip()
        gender = (request.form.get("gender") or "").strip()
        dob = (request.form.get("dob") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        email = (request.form.get("email") or "").strip()
        address = (request.form.get("address") or "").strip()
        city = (request.form.get("city") or "").strip()
        state = (request.form.get("state") or "").strip()
        country = (request.form.get("country") or "").strip()
        
        specialty = (request.form.get("specialty") or "").strip()
        sub_specialty = (request.form.get("sub_specialty") or "").strip()
        registration_number = (request.form.get("registration_number") or "").strip()
        medical_council = (request.form.get("medical_council") or "").strip()
        try:
            years_experience = int(request.form.get("years_experience") or 0)
        except ValueError:
            years_experience = 0
        highest_qualification = (request.form.get("highest_qualification") or "").strip()
        college_university = (request.form.get("college_university") or "").strip()
        hospital_clinic = (request.form.get("hospital_clinic") or "").strip()
        position = (request.form.get("position") or "").strip()
        try:
            consultation_fee = float(request.form.get("consultation_fee") or 0.0)
        except ValueError:
            consultation_fee = 0.0
        languages_spoken = (request.form.get("languages_spoken") or "").strip()
        
        working_days = ",".join(request.form.getlist("working_days"))
        consultation_hours = (request.form.get("consultation_hours") or "").strip()
        timezone = (request.form.get("timezone") or "").strip()
        online_consultation = 1 if request.form.get("online_consultation") == "yes" else 0
        offline_consultation = 1 if request.form.get("offline_consultation") == "yes" else 0
        
        bio = (request.form.get("bio") or "").strip()
        expertise = (request.form.get("expertise") or "").strip()
        certifications = (request.form.get("certifications") or "").strip()
        awards = (request.form.get("awards") or "").strip()
        
        if not full_name or not specialty:
            flash("Full name and medical specialty are required.")
            return render_template("doctor_profile.html", theme=theme, profile=profile)
            
        # Update users table
        db.execute(
            """
            UPDATE users 
            SET full_name = ?, doctor_specialty = ?, doctor_phone = ?, doctor_clinic = ?, doctor_bio = ?
            WHERE id = ?
            """,
            (
                encrypt_text(full_name),
                specialty,
                phone,
                hospital_clinic,
                bio,
                current_user.id
            )
        )
        
        # Update or insert into doctor_profiles
        exists = db.execute("SELECT 1 FROM doctor_profiles WHERE user_id = ?", (current_user.id,)).fetchone()
        if exists:
            db.execute(
                """
                UPDATE doctor_profiles SET
                  full_name=?, profile_photo=?, gender=?, dob=?, phone=?, email=?, address=?,
                  city=?, state=?, country=?, specialty=?, sub_specialty=?, registration_number=?,
                  medical_council=?, years_experience=?, highest_qualification=?, college_university=?,
                  hospital_clinic=?, position=?, consultation_fee=?, languages_spoken=?, working_days=?,
                  consultation_hours=?, timezone=?, online_consultation=?, offline_consultation=?,
                  bio=?, expertise=?, certifications=?, awards=?
                WHERE user_id = ?
                """,
                (
                    full_name, profile_photo, gender, dob, phone, email, address, city, state, country,
                    specialty, sub_specialty, registration_number, medical_council, years_experience,
                    highest_qualification, college_university, hospital_clinic, position, consultation_fee, languages_spoken,
                    working_days, consultation_hours, timezone, online_consultation, offline_consultation,
                    bio, expertise, certifications, awards, current_user.id
                )
            )
        else:
            db.execute(
                """
                INSERT INTO doctor_profiles (
                  user_id, full_name, profile_photo, gender, dob, phone, email, address, city, state, country,
                  specialty, sub_specialty, registration_number, medical_council, years_experience,
                  highest_qualification, college_university, hospital_clinic, position, consultation_fee, languages_spoken,
                  working_days, consultation_hours, timezone, online_consultation, offline_consultation,
                  bio, expertise, certifications, awards
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    current_user.id, full_name, profile_photo, gender, dob, phone, email, address, city, state, country,
                    specialty, sub_specialty, registration_number, medical_council, years_experience,
                    highest_qualification, college_university, hospital_clinic, position, consultation_fee, languages_spoken,
                    working_days, consultation_hours, timezone, online_consultation, offline_consultation,
                    bio, expertise, certifications, awards
                )
            )
        
        # Log activity
        db.execute(
            "INSERT INTO doctor_activity_logs (doctor_user_id, activity_type, description, created_at) VALUES (?, ?, ?, ?)",
            (current_user.id, "profile_update", "Doctor updated profile information.", now_ts())
        )
        
        db.commit()
        flash("Your profile was updated.")
        return redirect(url_for("core.doctor_profile"))
        
    return render_template("doctor_profile.html", theme=theme, profile=profile)


@core_bp.get("/doctor/emergency-scan")
@login_required
def doctor_emergency_scan():
    if _portal_role(current_user.id) != "doctor":
        flash("That tool is for doctor accounts only.")
        return redirect(url_for("core.dashboard"))
    return redirect(url_for("emergency.doctor_emergency_scanner"))


@core_bp.route("/doctor/patient/new", methods=["GET", "POST"])
@login_required
def doctor_patient_new():
    redir = _require_doctor()
    if redir:
        return redir
    theme = _portal_theme(current_user.id, current_user.username)
    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        notes = (request.form.get("notes") or "").strip()
        if len(full_name) < 2:
            flash("Please enter the patient’s name.")
            return render_template("doctor_patient_form.html", theme=theme)
        db = get_db()
        db.execute(
            """
            INSERT INTO doctor_patients (doctor_user_id, full_name, phone, notes, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (current_user.id, full_name, phone, notes, now_ts()),
        )
        db.commit()
        flash(f"Patient “{full_name}” was added to your list.")
        return redirect(url_for("core.doctor_dashboard"))
    return render_template("doctor_patient_form.html", theme=theme)


@core_bp.route("/doctor/prescription/new", methods=["GET", "POST"])
@login_required
def doctor_prescription_new():
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    patients = db.execute("SELECT full_name FROM doctor_patients WHERE doctor_user_id = ? ORDER BY full_name ASC", (current_user.id,)).fetchall()
    templates = db.execute("SELECT * FROM prescription_templates WHERE doctor_user_id = ? ORDER BY template_name ASC", (current_user.id,)).fetchall()
    if request.method == "POST":
        patient_name = (request.form.get("patient_name") or "").strip()
        medicine_name = (request.form.get("medicine_name") or "").strip()
        dosage = (request.form.get("dosage") or "").strip()
        frequency = (request.form.get("frequency") or "").strip()
        notes = (request.form.get("notes") or "").strip()
        try:
            duration_days = max(0, int((request.form.get("duration_days") or "0").strip() or 0))
        except ValueError:
            duration_days = 0
        sent_to_patient = 1 if request.form.get("send_to_patient") else 0
        if len(patient_name) < 2 or len(medicine_name) < 2:
            flash("Patient name and medicine are required.")
            return render_template(
                "doctor_prescription_form.html",
                theme=theme,
                patients=patients,
                templates=templates,
            )
        db.execute(
            """
            INSERT INTO doctor_prescriptions
            (doctor_user_id, patient_name, medicine_name, dosage, frequency, notes, duration_days, sent_to_patient, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                current_user.id,
                patient_name,
                medicine_name,
                dosage,
                frequency,
                notes,
                duration_days,
                sent_to_patient,
                now_ts(),
            ),
        )
        db.execute(
            "INSERT INTO doctor_activity_logs (doctor_user_id, activity_type, description, created_at) VALUES (?, ?, ?, ?)",
            (current_user.id, "prescription_add", f"Prescribed {medicine_name} to {patient_name}.", now_ts())
        )
        db.commit()
        msg = f"Prescription saved for {patient_name}: {medicine_name}"
        if duration_days:
            msg += f" ({duration_days} day course)."
        if sent_to_patient:
            msg += " Marked as sent to patient (visible in your records; patient app delivery is coming soon)."
        flash(msg + ".")
        return redirect(url_for("core.doctor_dashboard"))
    return render_template("doctor_prescription_form.html", theme=theme, patients=patients, templates=templates)


@core_bp.route("/doctor/consultation/new", methods=["GET", "POST"])
@login_required
def doctor_consultation_new():
    redir = _require_doctor()
    if redir:
        return redir
    theme = _portal_theme(current_user.id, current_user.username)
    db = get_db()
    patients = db.execute(
        "SELECT full_name FROM doctor_patients WHERE doctor_user_id = ? ORDER BY full_name ASC",
        (current_user.id,),
    ).fetchall()
    if request.method == "POST":
        patient_name = (request.form.get("patient_name") or "").strip()
        chief = (request.form.get("chief_complaint") or "").strip()
        notes = (request.form.get("notes") or "").strip()
        if len(patient_name) < 2:
            flash("Please enter the patient’s name.")
            return render_template(
                "doctor_consultation_form.html",
                theme=theme,
                patients=patients,
            )
        db.execute(
            """
            INSERT INTO doctor_consultations
            (doctor_user_id, patient_name, chief_complaint, notes, status, created_at)
            VALUES (?, ?, ?, ?, 'in_progress', ?)
            """,
            (current_user.id, patient_name, chief, notes, now_ts()),
        )
        db.commit()
        flash(f"Consultation started for {patient_name}. You can document findings in your EHR.")
        return redirect(url_for("core.doctor_dashboard"))
    return render_template("doctor_consultation_form.html", theme=theme, patients=patients)


@core_bp.route("/doctor/appointment/new", methods=["GET", "POST"])
@login_required
def doctor_appointment_new():
    redir = _require_doctor()
    if redir:
        return redir
    theme = _portal_theme(current_user.id, current_user.username)
    if request.method == "POST":
        patient_name = (request.form.get("patient_name") or "").strip()
        reason = (request.form.get("reason") or "").strip()
        date_s = (request.form.get("visit_date") or "").strip()
        time_s = (request.form.get("visit_time") or "").strip()
        status = (request.form.get("status") or "scheduled").strip()
        if status not in ("scheduled", "waiting", "in_progress"):
            status = "scheduled"
        if len(patient_name) < 2 or not date_s or not time_s:
            flash("Patient name, date, and time are required.")
            return render_template("doctor_appointment_form.html", theme=theme)
        try:
            dt = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M")
            visit_ts = int(dt.timestamp())
        except ValueError:
            flash("Invalid date or time.")
            return render_template("doctor_appointment_form.html", theme=theme)
        db = get_db()
        db.execute(
            """
            INSERT INTO doctor_appointments
            (doctor_user_id, patient_name, reason, visit_ts, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (current_user.id, patient_name, reason, visit_ts, status, now_ts()),
        )
        db.commit()
        flash(f"Appointment scheduled for {patient_name}.")
        return redirect(url_for("core.doctor_dashboard"))
    return render_template("doctor_appointment_form.html", theme=theme)


@core_bp.route("/doctor/appointments", methods=["GET"])
@login_required
def doctor_appointments_all():
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = int(current_user.id)
    
    # Filters
    q = (request.args.get("q") or "").strip()
    status_filter = (request.args.get("status") or "").strip()
    date_filter = (request.args.get("date") or "").strip()
    sort_by = (request.args.get("sort") or "time_asc").strip()
    
    try:
        page = int(request.args.get("page") or 1)
    except ValueError:
        page = 1
    per_page = 10
    offset = (page - 1) * per_page
    
    sql = "FROM doctor_appointments WHERE doctor_user_id = ?"
    params = [uid]
    
    if q:
        sql += " AND (patient_name LIKE ? OR reason LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like])
        
    if status_filter:
        sql += " AND status = ?"
        params.append(status_filter)
        
    if date_filter == "today":
        t0, t1 = _today_bounds()
        sql += " AND visit_ts >= ? AND visit_ts < ?"
        params.extend([t0, t1])
        
    # Count query
    total_count = db.execute(f"SELECT COUNT(*) AS c {sql}", tuple(params)).fetchone()["c"]
    
    # Sorting
    if sort_by == "time_desc":
        sql += " ORDER BY visit_ts DESC"
    elif sort_by == "name_asc":
        sql += " ORDER BY patient_name ASC"
    elif sort_by == "name_desc":
        sql += " ORDER BY patient_name DESC"
    else:
        sql += " ORDER BY visit_ts ASC"
        
    sql_limit = f"SELECT * {sql} LIMIT ? OFFSET ?"
    params.extend([per_page, offset])
    
    rows = db.execute(sql_limit, tuple(params)).fetchall()
    
    items = []
    for row in rows:
        st = row["status"] or "scheduled"
        items.append({
            "id": row["id"],
            "name": row["patient_name"],
            "sub": (row["reason"] or "").strip() or "Visit",
            "when": datetime.fromtimestamp(int(row["visit_ts"])).strftime("%Y-%m-%d %I:%M %p"),
            "status": st,
            "duration": row["duration_minutes"] or 30
        })
        
    total_pages = (total_count + per_page - 1) // per_page
    
    return render_template(
        "doctor_appointments_list.html",
        theme=theme,
        items=items,
        q=q,
        status_filter=status_filter,
        date_filter=date_filter,
        sort=sort_by,
        page=page,
        total_pages=total_pages,
        total_count=total_count
    )


@core_bp.get("/analytics")
@login_required
def patient_analytics():
    if _portal_role(current_user.id) == "doctor":
        return redirect(url_for("core.doctor_analytics"))
    
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = current_user.id
    
    h_data = _get_health_score_data(uid)
    
    last_log = db.execute("SELECT logged_at FROM vitals_logs WHERE user_id = ? ORDER BY logged_at DESC LIMIT 1", (uid,)).fetchone()
    vitals_not_updated_7_days = False
    if last_log and last_log["logged_at"]:
        try:
            dt = datetime.strptime(last_log["logged_at"][:16], "%Y-%m-%dT%H:%M")
            if (datetime.now() - dt).days >= 7:
                vitals_not_updated_7_days = True
        except Exception:
            pass
    else:
        vitals_not_updated_7_days = True
        
    if vitals_not_updated_7_days:
        trend = "Needs Attention"
    elif h_data["value"] >= 80:
        trend = "Improving"
    elif h_data["value"] >= 60:
        trend = "Stable"
    else:
        trend = "Needs Attention"
        
    ai_summary = []
    
    if vitals_not_updated_7_days:
        ai_summary.append("You have not updated your vitals in the last 7 days.")
        
    latest_vit = db.execute(
        "SELECT bp_systolic, bp_diastolic, sugar, heart_rate, sleep_hours, water_intake FROM vitals_logs WHERE user_id = ? ORDER BY logged_at DESC LIMIT 1",
        (uid,)
    ).fetchone()
    
    if latest_vit:
        sys = latest_vit["bp_systolic"]
        dia = latest_vit["bp_diastolic"]
        if sys and dia:
            if sys < 130 and dia < 85:
                ai_summary.append("Your blood pressure has remained stable and healthy.")
            else:
                ai_summary.append("Your blood pressure readings are slightly elevated.")
        sugar_val = latest_vit["sugar"]
        if sugar_val:
            if sugar_val < 140:
                ai_summary.append("Your blood sugar level is within a healthy range.")
            else:
                ai_summary.append("Your blood sugar readings are slightly above your average.")
        hr = latest_vit["heart_rate"]
        if hr:
            if 60 <= hr <= 100:
                ai_summary.append("Your heart rate is within a healthy range.")
            else:
                ai_summary.append("Your heart rate shows slight fluctuations.")
        sleep = latest_vit["sleep_hours"]
        if sleep:
            if sleep >= 7:
                ai_summary.append("Your sleep duration has improved this week.")
            else:
                ai_summary.append("Your sleep duration is slightly below the recommended target.")
                
    if not ai_summary:
        ai_summary.append("Log your daily vitals to generate customized AI insights.")
        
    vitals_rows = db.execute(
        """
        SELECT logged_at, bp_systolic, bp_diastolic, sugar, heart_rate, bmi, sleep_hours, water_intake 
        FROM vitals_logs 
        WHERE user_id = ? 
        ORDER BY logged_at ASC
        """,
        (uid,)
    ).fetchall()
    
    vitals = []
    for r in vitals_rows:
        v = dict(r)
        if v["logged_at"]:
            try:
                dt = datetime.strptime(v["logged_at"][:16], "%Y-%m-%dT%H:%M")
                v["date_label"] = dt.strftime("%b %d")
            except ValueError:
                v["date_label"] = v["logged_at"]
        else:
            v["date_label"] = "N/A"
        vitals.append(v)
        
    return render_template(
        "analytics.html",
        theme=theme,
        score=h_data["value"],
        category=h_data["category"],
        trend=trend,
        ai_summary=ai_summary,
        suggestions=h_data["suggestions"],
        vitals=vitals
    )


@core_bp.get("/doctor/analytics")
@login_required
def doctor_analytics():
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    payload = _analytics_payload(db, current_user.id)
    return render_template(
        "doctor_analytics.html",
        title="Health Analytics — MedVault",
        theme=theme,
        chart=payload,
    )


@core_bp.route("/doctor/ai-assistant", methods=["GET", "POST"])
@login_required
def doctor_ai_assistant():
    redir = _require_doctor()
    if redir:
        return redir
    theme = _portal_theme(current_user.id, current_user.username)
    result = None
    symptoms = ""
    age_s = ""
    if request.method == "POST":
        symptoms = (request.form.get("symptoms") or "").strip()
        age_s = (request.form.get("age_years") or "").strip()
        age_years: int | None = None
        if age_s:
            try:
                age_years = max(0, min(120, int(age_s)))
            except ValueError:
                age_years = None
        if len(symptoms) < 3:
            flash("Describe symptoms in a few words (at least 3 characters).")
        else:
            result = suggest_from_symptoms(symptoms, age_years)
    return render_template(
        "doctor_ai_assistant.html",
        title="AI Health Assistant — MedVault",
        theme=theme,
        symptoms=symptoms,
        age_years=age_s,
        result=result,
    )


@core_bp.route("/ai-assistant", methods=["GET", "POST"])
@login_required
def patient_ai_assistant():
    theme = _portal_theme(current_user.id, current_user.username)
    result = None
    symptoms = ""
    age_s = ""
    
    # Get patient's medical data for personalized AI responses
    db = get_db()
    patient_data = db.execute(
        """
        SELECT full_name, blood_group, allergies, medications, conditions
        FROM users WHERE id = ?
        """,
        (current_user.id,),
    ).fetchone()
    
    # Get recent medical history
    recent_files = db.execute(
        "SELECT filename, category FROM files WHERE user_id = ? ORDER BY uploaded_at DESC LIMIT 5",
        (current_user.id,),
    ).fetchall()
    
    recent_appointments = db.execute(
        "SELECT reason, status FROM appointments WHERE user_id = ? ORDER BY appointment_at DESC LIMIT 3",
        (current_user.id,),
    ).fetchall()
    
    current_prescriptions = db.execute(
        "SELECT medicine_name, dosage FROM prescriptions WHERE user_id = ? ORDER BY start_date DESC LIMIT 5",
        (current_user.id,),
    ).fetchall()
    
    if request.method == "POST":
        symptoms = (request.form.get("symptoms") or "").strip()
        age_s = (request.form.get("age_years") or "").strip()
        age_years: int | None = None
        if age_s:
            try:
                age_years = max(0, min(120, int(age_s)))
            except ValueError:
                age_years = None
        
        if len(symptoms) < 3:
            flash("Please describe your symptoms in at least 3 characters.")
        else:
            # Get personalized AI suggestion based on patient's medical history
            result = suggest_from_symptoms(symptoms, age_years, patient_data, recent_files, recent_appointments, current_prescriptions)
    
    return render_template(
        "patient_ai_assistant.html",
        title="AI Health Assistant — MedVault",
        theme=theme,
        symptoms=symptoms,
        age_years=age_s,
        result=result,
        patient_data=patient_data,
        recent_files=recent_files,
        recent_appointments=recent_appointments,
        current_prescriptions=current_prescriptions
    )


@core_bp.route("/book-appointment", methods=["GET", "POST"])
@login_required
def book_appointment():
    theme = _portal_theme(current_user.id, current_user.username)
    db = get_db()
    
    # Get available doctors
    doctors = db.execute(
        """
        SELECT id, name, specialization, contact 
        FROM doctors 
        WHERE user_id = ? 
        ORDER BY name
        """,
        (current_user.id,),
    ).fetchall()
    
    if request.method == "POST":
        doctor_id = request.form.get("doctor_id")
        appointment_date = request.form.get("appointment_date")
        appointment_time = request.form.get("appointment_time")
        reason = request.form.get("reason")
        
        if not all([doctor_id, appointment_date, appointment_time, reason]):
            flash("Please fill in all required fields.", "error")
        else:
            appointment_datetime = f"{appointment_date} {appointment_time}"
            
            # Prevent duplicate bookings (same doctor, same date/time)
            dup = db.execute(
                "SELECT 1 FROM appointments WHERE user_id = ? AND doctor_id = ? AND appointment_at = ? AND status != 'cancelled'",
                (current_user.id, int(doctor_id), appointment_datetime)
            ).fetchone()
            
            if dup:
                flash("You have already booked an appointment with this doctor at this time.", "error")
                return redirect(url_for("core.book_appointment"))
                
            # Insert patient-side appointment
            cursor = db.execute(
                """
                INSERT INTO appointments (user_id, doctor_id, appointment_at, reason, status, created_at)
                VALUES (?, ?, ?, ?, 'scheduled', ?)
                """,
                (current_user.id, int(doctor_id), appointment_datetime, reason, now_ts()),
            )
            appt_id = cursor.lastrowid
            
            # Find matching doctor user if any for sync booking
            fav_doctor = db.execute("SELECT name FROM doctors WHERE id = ?", (int(doctor_id),)).fetchone()
            doctor_user_id = None
            if fav_doctor:
                fav_name = fav_doctor["name"].lower().replace("dr.", "").replace("dr", "").strip()
                # Get all doctor users
                all_doc_users = db.execute("SELECT id, full_name, username FROM users WHERE portal_role = 'doctor'").fetchall()
                for doc_user in all_doc_users:
                    try:
                        dec_name = decrypt_text(doc_user["full_name"]).lower().replace("dr.", "").replace("dr", "").strip()
                    except Exception:
                        dec_name = doc_user["username"].lower()
                    if fav_name in dec_name or dec_name in fav_name or fav_name == doc_user["username"].lower():
                        doctor_user_id = doc_user["id"]
                        break
            
            # Fall back to first doctor user in the system if no exact match is found
            if not doctor_user_id:
                first_doc = db.execute("SELECT id FROM users WHERE portal_role = 'doctor' LIMIT 1").fetchone()
                if first_doc:
                    doctor_user_id = first_doc["id"]
            
            if doctor_user_id:
                patient_row = db.execute("SELECT full_name, username FROM users WHERE id = ?", (current_user.id,)).fetchone()
                try:
                    patient_name = decrypt_text(patient_row["full_name"]) or patient_row["username"]
                except Exception:
                    patient_name = patient_row["username"]
                
                try:
                    dt = datetime.strptime(appointment_datetime, "%Y-%m-%d %H:%M")
                    visit_ts = int(dt.timestamp())
                except ValueError:
                    visit_ts = now_ts()
                    
                db.execute(
                    """
                    INSERT INTO doctor_appointments (doctor_user_id, patient_name, reason, visit_ts, status, created_at)
                    VALUES (?, ?, ?, ?, 'scheduled', ?)
                    """,
                    (doctor_user_id, patient_name, reason, visit_ts, now_ts())
                )
                
            db.commit()
            flash("Appointment booked successfully.")
            return redirect(url_for("core.health_timeline"))
            
    return render_template(
        "book_appointment.html",
        title="Book Appointment — MedVault",
        theme=theme,
        doctors=doctors
    )


@core_bp.route("/api/patient/appointments/<int:appt_id>/cancel", methods=["POST"])
@login_required
def cancel_appointment(appt_id: int):
    db = get_db()
    appt = db.execute("SELECT * FROM appointments WHERE id = ? AND user_id = ?", (appt_id, current_user.id)).fetchone()
    if not appt:
        flash("Appointment not found.", "error")
        return redirect(url_for("core.dashboard"))
        
    db.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appt_id,))
    
    # Try cancelling corresponding doctor side appointment
    patient_row = db.execute("SELECT full_name, username FROM users WHERE id = ?", (current_user.id,)).fetchone()
    try:
        patient_name = decrypt_text(patient_row["full_name"]) or patient_row["username"]
    except Exception:
        patient_name = patient_row["username"]
        
    try:
        at = appt["appointment_at"]
        if "T" in at:
            dt = datetime.strptime(at[:16], "%Y-%m-%dT%H:%M")
        elif " " in at:
            dt = datetime.strptime(at[:16], "%Y-%m-%d %H:%M")
        else:
            dt = datetime.strptime(at[:10], "%Y-%m-%d")
        visit_ts = int(dt.timestamp())
        db.execute("UPDATE doctor_appointments SET status = 'cancelled' WHERE patient_name = ? AND visit_ts = ?", (patient_name, visit_ts))
    except Exception:
        pass
        
    db.commit()
    flash("Appointment cancelled successfully.")
    return redirect(url_for("core.dashboard"))


@core_bp.route("/api/patient/appointments/<int:appt_id>/reschedule", methods=["POST"])
@login_required
def reschedule_appointment(appt_id: int):
    new_date = request.form.get("appointment_date")
    new_time = request.form.get("appointment_time")
    if not new_date or not new_time:
        flash("New date and time are required.", "error")
        return redirect(url_for("core.dashboard"))
        
    db = get_db()
    appt = db.execute("SELECT * FROM appointments WHERE id = ? AND user_id = ?", (appt_id, current_user.id)).fetchone()
    if not appt:
        flash("Appointment not found.", "error")
        return redirect(url_for("core.dashboard"))
        
    new_datetime = f"{new_date} {new_time}"
    db.execute("UPDATE appointments SET appointment_at = ? WHERE id = ?", (new_datetime, appt_id))
    
    # Update doctor side
    patient_row = db.execute("SELECT full_name, username FROM users WHERE id = ?", (current_user.id,)).fetchone()
    try:
        patient_name = decrypt_text(patient_row["full_name"]) or patient_row["username"]
    except Exception:
        patient_name = patient_row["username"]
        
    try:
        at = appt["appointment_at"]
        if "T" in at:
            dt = datetime.strptime(at[:16], "%Y-%m-%dT%H:%M")
        elif " " in at:
            dt = datetime.strptime(at[:16], "%Y-%m-%d %H:%M")
        else:
            dt = datetime.strptime(at[:10], "%Y-%m-%d")
        old_visit_ts = int(dt.timestamp())
        
        new_dt = datetime.strptime(new_datetime, "%Y-%m-%d %H:%M")
        new_visit_ts = int(new_dt.timestamp())
        
        db.execute("UPDATE doctor_appointments SET visit_ts = ? WHERE patient_name = ? AND visit_ts = ?", (new_visit_ts, patient_name, old_visit_ts))
    except Exception:
        pass
        
    db.commit()
    flash("Appointment rescheduled successfully.")
    return redirect(url_for("core.dashboard"))


@core_bp.get("/onboarding")
@login_required
def onboarding():
    db = get_db()
    row = db.execute("SELECT onboarding_done, portal_role FROM users WHERE id = ?", (int(current_user.id),)).fetchone()
    if row and row["onboarding_done"]:
        if row["portal_role"] == "doctor":
            return redirect(url_for("core.doctor_dashboard"))
        return redirect(url_for("core.dashboard"))
        
    if _portal_role(current_user.id) == "doctor":
        return render_template(
            "onboarding_doctor.html",
            theme=_portal_theme(current_user.id, current_user.username),
        )
    return render_template(
        "onboarding_patient.html",
        theme=_portal_theme(current_user.id, current_user.username),
    )


@core_bp.post("/onboarding/complete")
@login_required
def complete_onboarding():
    db = get_db()
    role = _portal_role(current_user.id)
    
    if role == "doctor":
        full_name = (request.form.get("full_name") or "").strip()
        profile_photo = (request.form.get("profile_photo") or "").strip()
        gender = (request.form.get("gender") or "").strip()
        dob = (request.form.get("dob") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        email = (request.form.get("email") or "").strip()
        address = (request.form.get("address") or "").strip()
        city = (request.form.get("city") or "").strip()
        state = (request.form.get("state") or "").strip()
        country = (request.form.get("country") or "").strip()
        
        specialty = (request.form.get("specialty") or "").strip()
        sub_specialty = (request.form.get("sub_specialty") or "").strip()
        registration_number = (request.form.get("registration_number") or "").strip()
        medical_council = (request.form.get("medical_council") or "").strip()
        try:
            years_experience = int(request.form.get("years_experience") or 0)
        except ValueError:
            years_experience = 0
        highest_qualification = (request.form.get("highest_qualification") or "").strip()
        college_university = (request.form.get("college_university") or "").strip()
        hospital_clinic = (request.form.get("hospital_clinic") or "").strip()
        position = (request.form.get("position") or "").strip()
        try:
            consultation_fee = float(request.form.get("consultation_fee") or 0.0)
        except ValueError:
            consultation_fee = 0.0
        languages_spoken = (request.form.get("languages_spoken") or "").strip()
        
        working_days = ",".join(request.form.getlist("working_days"))
        consultation_hours = (request.form.get("consultation_hours") or "").strip()
        timezone = (request.form.get("timezone") or "").strip()
        online_consultation = 1 if request.form.get("online_consultation") == "yes" else 0
        offline_consultation = 1 if request.form.get("offline_consultation") == "yes" else 0
        
        bio = (request.form.get("bio") or "").strip()
        expertise = (request.form.get("expertise") or "").strip()
        certifications = (request.form.get("certifications") or "").strip()
        awards = (request.form.get("awards") or "").strip()
        
        errors = []
        if not full_name:
            errors.append("Full name is required.")
        if not phone:
            errors.append("Phone number is required.")
        if not specialty:
            errors.append("Medical specialty is required.")
        if not registration_number:
            errors.append("Registration number is required.")
        if not medical_council:
            errors.append("Medical council is required.")
        if not highest_qualification:
            errors.append("Highest qualification is required.")
        if years_experience < 0:
            errors.append("Years of experience must be a non-negative number.")
        if consultation_fee < 0:
            errors.append("Consultation fee must be a non-negative number.")
            
        if errors:
            for error in errors:
                flash(error, "error")
            return redirect(url_for("core.onboarding"))
            
        try:
            # Update users table
            db.execute(
                """
                UPDATE users 
                SET full_name = ?, doctor_specialty = ?, doctor_phone = ?, doctor_clinic = ?, doctor_bio = ?, onboarding_done = 1 
                WHERE id = ?
                """,
                (
                    encrypt_text(full_name),
                    specialty,
                    phone,
                    hospital_clinic,
                    bio,
                    int(current_user.id)
                )
            )
            
            # Update or insert into doctor_profiles
            row = db.execute("SELECT 1 FROM doctor_profiles WHERE user_id = ?", (int(current_user.id),)).fetchone()
            if row:
                db.execute(
                    """
                    UPDATE doctor_profiles SET
                      full_name=?, profile_photo=?, gender=?, dob=?, phone=?, email=?, address=?,
                      city=?, state=?, country=?, specialty=?, sub_specialty=?, registration_number=?,
                      medical_council=?, years_experience=?, highest_qualification=?, college_university=?,
                      hospital_clinic=?, position=?, consultation_fee=?, languages_spoken=?, working_days=?,
                      consultation_hours=?, timezone=?, online_consultation=?, offline_consultation=?,
                      bio=?, expertise=?, certifications=?, awards=?
                    WHERE user_id = ?
                    """,
                    (
                        full_name, profile_photo, gender, dob, phone, email, address, city, state, country,
                        specialty, sub_specialty, registration_number, medical_council, years_experience,
                        highest_qualification, college_university, hospital_clinic, position, consultation_fee, languages_spoken,
                        working_days, consultation_hours, timezone, online_consultation, offline_consultation,
                        bio, expertise, certifications, awards, int(current_user.id)
                    )
                )
            else:
                db.execute(
                    """
                    INSERT INTO doctor_profiles (
                      user_id, full_name, profile_photo, gender, dob, phone, email, address, city, state, country,
                      specialty, sub_specialty, registration_number, medical_council, years_experience,
                      highest_qualification, college_university, hospital_clinic, position, consultation_fee, languages_spoken,
                      working_days, consultation_hours, timezone, online_consultation, offline_consultation,
                      bio, expertise, certifications, awards
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(current_user.id), full_name, profile_photo, gender, dob, phone, email, address, city, state, country,
                        specialty, sub_specialty, registration_number, medical_council, years_experience,
                        highest_qualification, college_university, hospital_clinic, position, consultation_fee, languages_spoken,
                        working_days, consultation_hours, timezone, online_consultation, offline_consultation,
                        bio, expertise, certifications, awards
                    )
                )
            
            # Log activity
            db.execute(
                "INSERT INTO doctor_activity_logs (doctor_user_id, activity_type, description, created_at) VALUES (?, ?, ?, ?)",
                (int(current_user.id), "onboarding", "Doctor completed first-time profile onboarding setup.", now_ts())
            )
            
            db.commit()
            flash("Onboarding complete. Welcome to your Clinical Portal!")
            return redirect(url_for("core.doctor_dashboard"))
        except Exception as e:
            db.rollback()
            current_app.logger.error(f"Error completing doctor onboarding: {e}", exc_info=True)
            flash(f"A database error occurred during onboarding: {str(e)}. Please try again.")
            return redirect(url_for("core.onboarding"))
            
    else:
        # Get patient fields
        full_name = (request.form.get("full_name") or "").strip()
        try:
            age = int(request.form.get("age") or 0)
        except ValueError:
            age = 0
        gender = (request.form.get("gender") or "").strip()
        dob = (request.form.get("dob") or "").strip()
        blood_group = (request.form.get("blood_group") or "").strip()
        try:
            height = float(request.form.get("height") or 0.0)
        except ValueError:
            height = 0.0
        try:
            weight = float(request.form.get("weight") or 0.0)
        except ValueError:
            weight = 0.0
            
        phone = (request.form.get("phone") or "").strip()
        email = (request.form.get("email") or "").strip()
        address = (request.form.get("address") or "").strip()
        emergency_contact_name = (request.form.get("emergency_contact_name") or "").strip()
        emergency_contact_number = (request.form.get("emergency_contact_number") or "").strip()
        
        existing_diseases = (request.form.get("existing_diseases") or "").strip()
        current_medications = (request.form.get("current_medications") or "").strip()
        allergies = (request.form.get("allergies") or "").strip()
        previous_surgeries = (request.form.get("previous_surgeries") or "").strip()
        family_medical_history = (request.form.get("family_medical_history") or "").strip()
        smoking_status = (request.form.get("smoking_status") or "").strip()
        alcohol_consumption = (request.form.get("alcohol_consumption") or "").strip()
        
        exercise_frequency = (request.form.get("exercise_frequency") or "").strip()
        try:
            sleep_duration = float(request.form.get("sleep_duration") or 0.0)
        except ValueError:
            sleep_duration = 0.0
        diet_preference = (request.form.get("diet_preference") or "").strip()
        try:
            water_intake = float(request.form.get("water_intake") or 0.0)
        except ValueError:
            water_intake = 0.0
        occupation = (request.form.get("occupation") or "").strip()
        
        preferred_language = (request.form.get("preferred_language") or "").strip()
        preferred_consultation_mode = (request.form.get("preferred_consultation_mode") or "").strip()
        
        # Validation
        errors = []
        if not full_name:
            errors.append("Full name is required.")
        if age <= 0:
            errors.append("Valid age is required.")
        if not dob:
            errors.append("Date of birth is required.")
        if not phone:
            errors.append("Phone number is required.")
        if not email:
            errors.append("Email address is required.")
        if not emergency_contact_name or not emergency_contact_number:
            errors.append("Emergency contact details are required.")
        if height < 0:
            errors.append("Height cannot be negative.")
        if weight < 0:
            errors.append("Weight cannot be negative.")
        if sleep_duration < 0:
            errors.append("Sleep duration cannot be negative.")
        if water_intake < 0:
            errors.append("Water intake cannot be negative.")
            
        if errors:
            for error in errors:
                flash(error, "error")
            return redirect(url_for("core.onboarding"))
            
        try:
            # Update users table
            db.execute(
                """
                UPDATE users 
                SET full_name = ?, blood_group = ?, allergies = ?, medications = ?, conditions = ?, onboarding_done = 1 
                WHERE id = ?
                """,
                (
                    encrypt_text(full_name),
                    encrypt_text(blood_group),
                    encrypt_text(allergies),
                    encrypt_text(current_medications),
                    encrypt_text(existing_diseases),
                    current_user.id
                )
            )
            
            # Update or insert into patient_profiles
            row = db.execute("SELECT 1 FROM patient_profiles WHERE user_id = ?", (current_user.id,)).fetchone()
            if row:
                db.execute(
                    """
                    UPDATE patient_profiles SET
                      full_name=?, age=?, gender=?, dob=?, blood_group=?, height=?, weight=?,
                      phone=?, email=?, address=?, emergency_contact_name=?, emergency_contact_number=?,
                      existing_diseases=?, current_medications=?, allergies=?, previous_surgeries=?,
                      family_medical_history=?, smoking_status=?, alcohol_consumption=?, exercise_frequency=?,
                      sleep_duration=?, diet_preference=?, water_intake=?, occupation=?, preferred_language=?,
                      preferred_consultation_mode=?
                    WHERE user_id = ?
                    """,
                    (
                        full_name, age, gender, dob, blood_group, height, weight,
                        phone, email, address, emergency_contact_name, emergency_contact_number,
                        existing_diseases, current_medications, allergies, previous_surgeries,
                        family_medical_history, smoking_status, alcohol_consumption, exercise_frequency,
                        sleep_duration, diet_preference, water_intake, occupation, preferred_language,
                        preferred_consultation_mode, current_user.id
                    )
                )
            else:
                db.execute(
                    """
                    INSERT INTO patient_profiles (
                      user_id, full_name, age, gender, dob, blood_group, height, weight,
                      phone, email, address, emergency_contact_name, emergency_contact_number,
                      existing_diseases, current_medications, allergies, previous_surgeries,
                      family_medical_history, smoking_status, alcohol_consumption, exercise_frequency,
                      sleep_duration, diet_preference, water_intake, occupation, preferred_language,
                      preferred_consultation_mode
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        current_user.id, full_name, age, gender, dob, blood_group, height, weight,
                        phone, email, address, emergency_contact_name, emergency_contact_number,
                        existing_diseases, current_medications, allergies, previous_surgeries,
                        family_medical_history, smoking_status, alcohol_consumption, exercise_frequency,
                        sleep_duration, diet_preference, water_intake, occupation, preferred_language,
                        preferred_consultation_mode
                    )
                )
            
            db.commit()
            flash("Onboarding complete. Welcome to MedVault!")
            return redirect(url_for("core.dashboard"))
        except Exception as e:
            db.rollback()
            current_app.logger.error(f"Error completing patient onboarding: {e}", exc_info=True)
            flash(f"A database error occurred during onboarding: {str(e)}. Please try again.")
            return redirect(url_for("core.onboarding"))


@core_bp.post("/profile")
@login_required
def update_profile():
    db = get_db()
    role = _portal_role(current_user.id)
    
    if role == "patient":
        # Get patient fields
        full_name = (request.form.get("full_name") or "").strip()
        try:
            age = int(request.form.get("age") or 0)
        except ValueError:
            age = 0
        gender = (request.form.get("gender") or "").strip()
        dob = (request.form.get("dob") or "").strip()
        blood_group = (request.form.get("blood_group") or "").strip()
        try:
            height = float(request.form.get("height") or 0.0)
        except ValueError:
            height = 0.0
        try:
            weight = float(request.form.get("weight") or 0.0)
        except ValueError:
            weight = 0.0
            
        phone = (request.form.get("phone") or "").strip()
        email = (request.form.get("email") or "").strip()
        address = (request.form.get("address") or "").strip()
        emergency_contact_name = (request.form.get("emergency_contact_name") or "").strip()
        emergency_contact_number = (request.form.get("emergency_contact_number") or "").strip()
        
        existing_diseases = (request.form.get("existing_diseases") or "").strip()
        current_medications = (request.form.get("current_medications") or "").strip()
        allergies = (request.form.get("allergies") or "").strip()
        previous_surgeries = (request.form.get("previous_surgeries") or "").strip()
        family_medical_history = (request.form.get("family_medical_history") or "").strip()
        smoking_status = (request.form.get("smoking_status") or "").strip()
        alcohol_consumption = (request.form.get("alcohol_consumption") or "").strip()
        
        exercise_frequency = (request.form.get("exercise_frequency") or "").strip()
        try:
            sleep_duration = float(request.form.get("sleep_duration") or 0.0)
        except ValueError:
            sleep_duration = 0.0
        diet_preference = (request.form.get("diet_preference") or "").strip()
        try:
            water_intake = float(request.form.get("water_intake") or 0.0)
        except ValueError:
            water_intake = 0.0
        occupation = (request.form.get("occupation") or "").strip()
        
        preferred_language = (request.form.get("preferred_language") or "").strip()
        preferred_consultation_mode = (request.form.get("preferred_consultation_mode") or "").strip()
        
        errors = []
        if not full_name:
            errors.append("Full name is required.")
        if age <= 0:
            errors.append("Valid age is required.")
        if not dob:
            errors.append("Date of birth is required.")
        if not phone:
            errors.append("Phone number is required.")
        if not email:
            errors.append("Email address is required.")
        if not emergency_contact_name or not emergency_contact_number:
            errors.append("Emergency contact details are required.")
        if height < 0:
            errors.append("Height cannot be negative.")
        if weight < 0:
            errors.append("Weight cannot be negative.")
            
        if errors:
            for error in errors:
                flash(error, "error")
            return _redirect_home()
            
        try:
            db.execute(
                """
                UPDATE users 
                SET full_name = ?, blood_group = ?, allergies = ?, medications = ?, conditions = ?
                WHERE id = ?
                """,
                (
                    encrypt_text(full_name),
                    encrypt_text(blood_group),
                    encrypt_text(allergies),
                    encrypt_text(current_medications),
                    encrypt_text(existing_diseases),
                    current_user.id
                )
            )
            
            # Update or insert into patient_profiles
            row = db.execute("SELECT 1 FROM patient_profiles WHERE user_id = ?", (current_user.id,)).fetchone()
            if row:
                db.execute(
                    """
                    UPDATE patient_profiles SET
                      full_name=?, age=?, gender=?, dob=?, blood_group=?, height=?, weight=?,
                      phone=?, email=?, address=?, emergency_contact_name=?, emergency_contact_number=?,
                      existing_diseases=?, current_medications=?, allergies=?, previous_surgeries=?,
                      family_medical_history=?, smoking_status=?, alcohol_consumption=?, exercise_frequency=?,
                      sleep_duration=?, diet_preference=?, water_intake=?, occupation=?, preferred_language=?,
                      preferred_consultation_mode=?
                    WHERE user_id = ?
                    """,
                    (
                        full_name, age, gender, dob, blood_group, height, weight,
                        phone, email, address, emergency_contact_name, emergency_contact_number,
                        existing_diseases, current_medications, allergies, previous_surgeries,
                        family_medical_history, smoking_status, alcohol_consumption, exercise_frequency,
                        sleep_duration, diet_preference, water_intake, occupation, preferred_language,
                        preferred_consultation_mode, current_user.id
                    )
                )
            else:
                db.execute(
                    """
                    INSERT INTO patient_profiles (
                      user_id, full_name, age, gender, dob, blood_group, height, weight,
                      phone, email, address, emergency_contact_name, emergency_contact_number,
                      existing_diseases, current_medications, allergies, previous_surgeries,
                      family_medical_history, smoking_status, alcohol_consumption, exercise_frequency,
                      sleep_duration, diet_preference, water_intake, occupation, preferred_language,
                      preferred_consultation_mode
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        current_user.id, full_name, age, gender, dob, blood_group, height, weight,
                        phone, email, address, emergency_contact_name, emergency_contact_number,
                        existing_diseases, current_medications, allergies, previous_surgeries,
                        family_medical_history, smoking_status, alcohol_consumption, exercise_frequency,
                        sleep_duration, diet_preference, water_intake, occupation, preferred_language,
                        preferred_consultation_mode
                    )
                )
            db.commit()
            flash("Profile updated securely.")
        except Exception as e:
            db.rollback()
            current_app.logger.error(f"Error updating patient profile: {e}", exc_info=True)
            flash(f"A database error occurred: {str(e)}")
            
    else:
        db.execute(
            """
            UPDATE users
            SET full_name = ?, blood_group = ?, allergies = ?, medications = ?, conditions = ?
            WHERE id = ?
            """,
            (
                encrypt_text((request.form.get("full_name") or "").strip()),
                encrypt_text((request.form.get("blood_group") or "").strip()),
                encrypt_text((request.form.get("allergies") or "").strip()),
                encrypt_text((request.form.get("medications") or "").strip()),
                encrypt_text((request.form.get("conditions") or "").strip()),
                current_user.id,
            ),
        )
        db.commit()
        flash("Profile updated securely.")
        
    return _redirect_home()


def map_to_standard_category(ai_category: str, filename: str) -> str:
    fn = filename.lower()
    if ai_category == "Prescription" or "prescription" in fn or "rx" in fn:
        return "Prescriptions"
    elif ai_category == "Blood Report" or "blood" in fn or "cbc" in fn:
        return "Blood Test Reports"
    elif "xray" in fn or "x-ray" in fn:
        return "X-Ray"
    elif "mri" in fn:
        return "MRI"
    elif "ct" in fn or "ctscan" in fn:
        return "CT Scan"
    elif "ecg" in fn or "ekg" in fn:
        return "ECG"
    elif "ultrasound" in fn or "usg" in fn:
        return "Ultrasound"
    elif ai_category == "Vaccination" or "vaccine" in fn or "immunization" in fn:
        return "Vaccination Records"
    elif ai_category == "Discharge Summary" or "discharge" in fn:
        return "Discharge Summary"
    elif "certificate" in fn or "sickleave" in fn:
        return "Medical Certificates"
    elif ai_category == "Insurance" or "insurance" in fn or "claim" in fn:
        return "Insurance Documents"
    else:
        return "Other Medical Documents"

@core_bp.post("/upload")
@login_required
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Please choose a file.")
        return redirect(request.referrer or url_for('core.dashboard'))
    if not allowed_file(f.filename):
        flash("Allowed file types: PDF, JPG, PNG.")
        return redirect(request.referrer or url_for('core.dashboard'))

    original = secure_filename(f.filename)
    stored_name = f"{current_user.id}_{now_ts()}_{secrets.token_hex(8)}_{original}"
    
    # Use writeable /tmp/uploads folder to store file temporarily for OCR
    temp_dir = os.path.join("/tmp", "uploads")
    os.makedirs(temp_dir, exist_ok=True)
    saved_path = os.path.join(temp_dir, stored_name)
    f.save(saved_path)
    
    # Calculate file size
    try:
        file_size = os.path.getsize(saved_path)
    except OSError:
        file_size = 0
        
    # Extract text from image files for AI categorization
    extracted_text = ""
    ext = original.rsplit(".", 1)[1].lower() if "." in original else ""
    file_type = ext.upper() if ext else "UNKNOWN"
    
    if ext in {"png", "jpg", "jpeg"}:
        extracted_text = extract_text_with_tesseract(saved_path)
        
    # Determine category
    form_category = (request.form.get("category") or "").strip()
    if form_category and form_category != "Auto-Detect":
        category = form_category
        confidence = 1.0
    else:
        ai_cat, confidence, metadata = ai_categorize_medical_file(original, extracted_text)
        category = map_to_standard_category(ai_cat, original)
        
    doc_category = (request.form.get("doc_category") or "").strip()
    doc_source = (request.form.get("doc_source") or "").strip()
    uploaded_by = "Doctor" if current_user.portal_role == "doctor" else "Patient"
    
    # Generate AI Document Summary
    ai_summary = generate_document_summary(original, extracted_text)

    # Upload to Supabase Storage if configured, else keep it in local uploads folder
    if is_supabase_configured():
        try:
            with open(saved_path, "rb") as file_bytes:
                content_type = f.content_type or "application/octet-stream"
                upload_file_to_supabase(file_bytes.read(), stored_name, content_type)
            stored_db_path = stored_name
        except Exception as e:
            flash(f"Failed to upload to Supabase: {str(e)}")
            try:
                os.remove(saved_path)
            except OSError:
                pass
            return redirect(request.referrer or url_for('core.dashboard'))
        # Clean up temp file
        try:
            os.remove(saved_path)
        except OSError:
            pass
    else:
        # Fallback to local UPLOAD_FOLDER
        final_dest = os.path.join(current_app.config["UPLOAD_FOLDER"], stored_name)
        os.makedirs(os.path.dirname(final_dest), exist_ok=True)
        if saved_path != final_dest:
            import shutil
            shutil.move(saved_path, final_dest)
        stored_db_path = final_dest

    db = get_db()
    db.execute(
        """
        INSERT INTO files (user_id, filename, stored_path, category, category_confidence, doc_category, doc_source, extracted_text, file_size, file_type, uploaded_by, ai_summary, uploaded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            current_user.id,
            original,
            stored_db_path,
            category,
            confidence,
            doc_category,
            doc_source,
            extracted_text,
            file_size,
            file_type,
            uploaded_by,
            ai_summary,
            now_ts(),
        ),
    )
    
    # Create notification for reports
    db.execute(
        """
        INSERT INTO patient_notifications (user_id, category, title, message, is_read, created_at)
        VALUES (?, 'Reports', 'New Document Uploaded', ?, 0, ?)
        """,
        (
            current_user.id,
            f"Your document '{original}' ({file_type}, {round(file_size/1024, 1) if file_size else 0} KB) was uploaded by {uploaded_by} under category '{category}'.",
            now_ts()
        )
    )
    db.commit()
    
    flash(f"File '{original}' uploaded and categorized under '{category}'.")
    return redirect(request.referrer or url_for('core.dashboard'))


@core_bp.get("/files/<int:file_id>/preview")
@login_required
def preview_file(file_id: int):
    db = get_db()
    row = db.execute(
        "SELECT filename, stored_path FROM files WHERE id = ? AND user_id = ?",
        (file_id, current_user.id),
    ).fetchone()
    if not row:
        abort(404)
        
    stored_path = row["stored_path"]
    
    import mimetypes
    mimetype, _ = mimetypes.guess_type(row["filename"])
    if not mimetype:
        mimetype = "application/octet-stream"
        
    if is_supabase_configured():
        from flask import Response
        try:
            stored_name = os.path.basename(stored_path)
            file_data = download_file_from_supabase(stored_name)
            return Response(
                file_data,
                mimetype=mimetype,
                headers={
                    "Content-Disposition": f"inline; filename={row['filename']}"
                }
            )
        except Exception as e:
            abort(500, description=f"Supabase download failed: {str(e)}")
            
    # Fallback to local files
    if not os.path.isabs(stored_path):
        stored_path = os.path.join(current_app.config["UPLOAD_FOLDER"], stored_path)
        
    directory = os.path.dirname(stored_path)
    filename = os.path.basename(stored_path)
    
    return send_from_directory(
        directory,
        filename,
        as_attachment=False,
        mimetype=mimetype
    )


@core_bp.post("/files/<int:file_id>/rename")
@login_required
def rename_file(file_id: int):
    new_name = request.form.get("new_filename", "").strip()
    if not new_name:
        return jsonify({"error": "Filename cannot be empty"}), 400
        
    db = get_db()
    row = db.execute("SELECT filename FROM files WHERE id = ? AND user_id = ?", (file_id, current_user.id)).fetchone()
    if not row:
        return jsonify({"error": "File not found"}), 404
        
    orig_ext = row["filename"].rsplit(".", 1)[-1].lower() if "." in row["filename"] else ""
    new_ext = new_name.rsplit(".", 1)[-1].lower() if "." in new_name else ""
    
    if orig_ext and orig_ext != new_ext:
        new_name = f"{new_name.rsplit('.', 1)[0]}.{orig_ext}"
        
    db.execute("UPDATE files SET filename = ? WHERE id = ? AND user_id = ?", (new_name, file_id, current_user.id))
    db.commit()
    return jsonify({"success": True, "new_filename": new_name})


@core_bp.get("/api/patient/files/<int:file_id>/summary")
@login_required
def get_file_summary(file_id: int):
    db = get_db()
    row = db.execute(
        "SELECT filename, ai_summary, category FROM files WHERE id = ? AND user_id = ?",
        (file_id, current_user.id)
    ).fetchone()
    if not row:
        return jsonify({"error": "File not found"}), 404
    return jsonify({
        "filename": row["filename"],
        "category": row["category"],
        "ai_summary": row["ai_summary"] or "No summary available for this file."
    })


@core_bp.get("/files/<int:file_id>/download")
@login_required
def download_file(file_id: int):
    db = get_db()
    row = db.execute(
        "SELECT filename, stored_path FROM files WHERE id = ? AND user_id = ?",
        (file_id, current_user.id),
    ).fetchone()
    if not row:
        abort(404)
        
    stored_path = row["stored_path"]
    
    if is_supabase_configured():
        from flask import Response
        try:
            stored_name = os.path.basename(stored_path)
            file_data = download_file_from_supabase(stored_name)
            return Response(
                file_data,
                mimetype="application/octet-stream",
                headers={
                    "Content-Disposition": f"attachment; filename={row['filename']}"
                }
            )
        except Exception as e:
            abort(500, description=f"Supabase download failed: {str(e)}")
            
    # Fallback to local files
    if not os.path.isabs(stored_path):
        stored_path = os.path.join(current_app.config["UPLOAD_FOLDER"], stored_path)
        
    directory = os.path.dirname(stored_path)
    filename = os.path.basename(stored_path)
    
    return send_from_directory(
        directory,
        filename,
        as_attachment=True,
        download_name=row["filename"],
    )


@core_bp.post("/files/<int:file_id>/delete")
@login_required
def delete_file(file_id: int):
    db = get_db()
    row = db.execute(
        "SELECT filename, stored_path FROM files WHERE id = ? AND user_id = ?",
        (file_id, current_user.id),
    ).fetchone()
    if not row:
        abort(404)
        
    stored_path = row["stored_path"]
    filename_orig = row["filename"]
    
    db.execute("DELETE FROM files WHERE id = ? AND user_id = ?", (file_id, current_user.id))
    
    # Create notification for file deletion
    db.execute(
        """
        INSERT INTO patient_notifications (user_id, category, title, message, is_read, created_at)
        VALUES (?, 'Reports', 'Document Deleted', ?, 0, ?)
        """,
        (current_user.id, f"Document '{filename_orig}' has been deleted.", now_ts())
    )
    db.commit()
    
    if is_supabase_configured():
        stored_name = os.path.basename(stored_path)
        try:
            delete_file_from_supabase(stored_name)
        except Exception as e:
            print(f"Failed to delete {stored_name} from Supabase: {e}")
    else:
        if not os.path.isabs(stored_path):
            stored_path = os.path.join(current_app.config["UPLOAD_FOLDER"], stored_path)
        try:
            os.remove(stored_path)
        except OSError:
            pass
            
    flash("File deleted.")
    return _redirect_home()


@core_bp.route("/doctor/leaves", methods=["GET", "POST"])
@login_required
def doctor_leaves():
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    if request.method == "POST":
        start_date = (request.form.get("start_date") or "").strip()
        end_date = (request.form.get("end_date") or "").strip()
        reason = (request.form.get("reason") or "").strip()
        if not start_date or not end_date:
            flash("Start date and End date are required.")
        else:
            db.execute(
                "INSERT INTO doctor_leaves (doctor_user_id, start_date, end_date, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                (current_user.id, start_date, end_date, reason, now_ts())
            )
            db.execute(
                "INSERT INTO doctor_activity_logs (doctor_user_id, activity_type, description, created_at) VALUES (?, ?, ?, ?)",
                (current_user.id, "leave_add", f"Added leave from {start_date} to {end_date}.", now_ts())
            )
            db.commit()
            flash("Leave scheduled successfully.")
            return redirect(url_for("core.doctor_leaves"))
            
    leaves = db.execute("SELECT id, start_date, end_date, reason FROM doctor_leaves WHERE doctor_user_id = ? ORDER BY start_date DESC", (current_user.id,)).fetchall()
    return render_template("doctor_leaves.html", theme=theme, leaves=leaves)


@core_bp.post("/doctor/leaves/<int:leave_id>/delete")
@login_required
def delete_doctor_leave(leave_id: int):
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    db.execute("DELETE FROM doctor_leaves WHERE id = ? AND doctor_user_id = ?", (leave_id, current_user.id))
    db.commit()
    flash("Leave entry removed.")
    return redirect(url_for("core.doctor_leaves"))


@core_bp.route("/doctor/prescription-templates", methods=["GET", "POST"])
@login_required
def doctor_prescription_templates():
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    if request.method == "POST":
        template_name = (request.form.get("template_name") or "").strip()
        medicine_name = (request.form.get("medicine_name") or "").strip()
        dosage = (request.form.get("dosage") or "").strip()
        frequency = (request.form.get("frequency") or "").strip()
        try:
            duration_days = int(request.form.get("duration_days") or 0)
        except ValueError:
            duration_days = 0
        notes = (request.form.get("notes") or "").strip()
        
        if not template_name or not medicine_name:
            flash("Template name and Medicine name are required.")
        else:
            db.execute(
                """
                INSERT INTO prescription_templates (doctor_user_id, template_name, medicine_name, dosage, frequency, duration_days, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (current_user.id, template_name, medicine_name, dosage, frequency, duration_days, notes, now_ts())
            )
            db.commit()
            flash("Prescription template saved.")
            return redirect(url_for("core.doctor_prescription_templates"))
            
    templates = db.execute("SELECT * FROM prescription_templates WHERE doctor_user_id = ? ORDER BY template_name ASC", (current_user.id,)).fetchall()
    return render_template("doctor_prescription_templates.html", theme=theme, templates=templates)


@core_bp.post("/doctor/prescription-templates/<int:template_id>/delete")
@login_required
def delete_prescription_template(template_id: int):
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    db.execute("DELETE FROM prescription_templates WHERE id = ? AND doctor_user_id = ?", (template_id, current_user.id))
    db.commit()
    flash("Prescription template deleted.")
    return redirect(url_for("core.doctor_prescription_templates"))


@core_bp.route("/doctor/patient-notes", methods=["GET", "POST"])
@login_required
def doctor_patient_notes_route():
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    if request.method == "POST":
        patient_name = (request.form.get("patient_name") or "").strip()
        notes = (request.form.get("notes") or "").strip()
        if not patient_name or not notes:
            flash("Patient name and notes are required.")
        else:
            db.execute(
                "INSERT INTO doctor_patient_notes (doctor_user_id, patient_name, notes, created_at) VALUES (?, ?, ?, ?)",
                (current_user.id, patient_name, notes, now_ts())
            )
            db.commit()
            flash("Progress note added.")
            return redirect(url_for("core.doctor_patient_notes_route"))
            
    notes_list = db.execute("SELECT * FROM doctor_patient_notes WHERE doctor_user_id = ? ORDER BY created_at DESC", (current_user.id,)).fetchall()
    patients = db.execute("SELECT full_name FROM doctor_patients WHERE doctor_user_id = ?", (current_user.id,)).fetchall()
    return render_template("doctor_patient_notes.html", theme=theme, notes_list=notes_list, patients=patients)


@core_bp.route("/doctor/reminders", methods=["GET", "POST"])
@login_required
def doctor_reminders_route():
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    if request.method == "POST":
        patient_name = (request.form.get("patient_name") or "").strip()
        title = (request.form.get("title") or "").strip()
        remind_at = (request.form.get("remind_at") or "").strip()
        if not patient_name or not title or not remind_at:
            flash("All fields are required.")
        else:
            db.execute(
                "INSERT INTO doctor_reminders (doctor_user_id, patient_name, title, remind_at, is_done, created_at) VALUES (?, ?, ?, ?, 0, ?)",
                (current_user.id, patient_name, title, remind_at, now_ts())
            )
            db.commit()
            flash("Follow-up reminder set.")
            return redirect(url_for("core.doctor_reminders_route"))
            
    reminders = db.execute("SELECT * FROM doctor_reminders WHERE doctor_user_id = ? ORDER BY remind_at ASC", (current_user.id,)).fetchall()
    patients = db.execute("SELECT full_name FROM doctor_patients WHERE doctor_user_id = ?", (current_user.id,)).fetchall()
    return render_template("doctor_reminders.html", theme=theme, reminders=reminders, patients=patients)


@core_bp.post("/doctor/reminders/<int:reminder_id>/toggle")
@login_required
def toggle_doctor_reminder(reminder_id: int):
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    row = db.execute("SELECT is_done FROM doctor_reminders WHERE id = ? AND doctor_user_id = ?", (reminder_id, current_user.id)).fetchone()
    if row:
        new_status = 1 if row["is_done"] == 0 else 0
        db.execute("UPDATE doctor_reminders SET is_done = ? WHERE id = ?", (new_status, reminder_id))
        db.commit()
        flash("Reminder status updated.")
    return redirect(url_for("core.doctor_reminders_route"))


@core_bp.route("/doctor/appointments-calendar")
@login_required
def doctor_appointments_calendar():
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    # Fetch all appointments
    appts = db.execute("SELECT * FROM doctor_appointments WHERE doctor_user_id = ? ORDER BY visit_ts ASC", (current_user.id,)).fetchall()
    
    calendar_events = []
    for a in appts:
        dt = datetime.fromtimestamp(int(a["visit_ts"]))
        calendar_events.append({
            "id": a["id"],
            "patient_name": a["patient_name"],
            "reason": a["reason"],
            "date": dt.strftime("%Y-%m-%d"),
            "time": dt.strftime("%I:%M %p"),
            "status": a["status"]
        })
        
    return render_template("doctor_appointments_calendar.html", theme=theme, events=calendar_events)


@core_bp.post("/doctor/appointments/<int:appt_id>/status")
@login_required
def update_appointment_status(appt_id: int):
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    status = (request.form.get("status") or "").strip()
    if status in ("scheduled", "waiting", "in_progress", "completed", "cancelled"):
        db.execute("UPDATE doctor_appointments SET status = ? WHERE id = ? AND doctor_user_id = ?", (status, appt_id, current_user.id))
        db.commit()
        flash("Appointment status updated.")
    return redirect(request.referrer or url_for("core.doctor_dashboard"))


@core_bp.get("/doctor/export/appointments")
@login_required
def export_appointments_csv():
    redir = _require_doctor()
    if redir:
        return redir
    import csv
    import io
    from flask import Response
    
    db = get_db()
    rows = db.execute("SELECT id, patient_name, reason, visit_ts, status, created_at FROM doctor_appointments WHERE doctor_user_id = ? ORDER BY visit_ts DESC", (current_user.id,)).fetchall()
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Appointment ID", "Patient Name", "Reason", "Date & Time", "Status", "Booked At"])
    
    for r in rows:
        dt = datetime.fromtimestamp(int(r["visit_ts"])).strftime("%Y-%m-%d %I:%M %p")
        created = datetime.fromtimestamp(int(r["created_at"])).strftime("%Y-%m-%d %I:%M %p")
        writer.writerow([r["id"], r["patient_name"], r["reason"], dt, r["status"], created])
        
    response = Response(output.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=appointments_export.csv"
    return response


@core_bp.get("/doctor/export/patients")
@login_required
def export_patients_csv():
    redir = _require_doctor()
    if redir:
        return redir
    import csv
    import io
    from flask import Response
    
    db = get_db()
    rows = db.execute("SELECT id, full_name, phone, notes, created_at FROM doctor_patients WHERE doctor_user_id = ? ORDER BY full_name ASC", (current_user.id,)).fetchall()
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Patient ID", "Full Name", "Phone", "Clinical Notes", "Added At"])
    
    for r in rows:
        added = datetime.fromtimestamp(int(r["created_at"])).strftime("%Y-%m-%d %I:%M %p")
        writer.writerow([r["id"], r["full_name"], r["phone"], r["notes"], added])
        
    response = Response(output.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=patient_registry_export.csv"
    return response


@core_bp.route("/doctor/patients", methods=["GET"])
@login_required
def doctor_patients():
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = int(current_user.id)
    
    q = (request.args.get("q") or "").strip()
    sort_by = (request.args.get("sort") or "name_asc").strip()
    
    try:
        page = int(request.args.get("page") or 1)
    except ValueError:
        page = 1
    per_page = 10
    offset = (page - 1) * per_page
    
    sql = "FROM doctor_patients WHERE doctor_user_id = ?"
    params = [uid]
    if q:
        sql += " AND (full_name LIKE ? OR notes LIKE ? OR phone LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like, like])
        
    total_count = db.execute(f"SELECT COUNT(*) AS c {sql}", tuple(params)).fetchone()["c"]
    
    if sort_by == "name_desc":
        sql += " ORDER BY full_name DESC"
    elif sort_by == "date_asc":
        sql += " ORDER BY created_at ASC"
    elif sort_by == "date_desc":
        sql += " ORDER BY created_at DESC"
    else:
        sql += " ORDER BY full_name ASC"
        
    sql_limit = f"SELECT * {sql} LIMIT ? OFFSET ?"
    params.extend([per_page, offset])
    
    rows = db.execute(sql_limit, tuple(params)).fetchall()
    
    patients = []
    for r in rows:
        dt = datetime.fromtimestamp(int(r["created_at"])).strftime("%b %d, %Y")
        patients.append({
            "id": r["id"],
            "full_name": r["full_name"],
            "phone": r["phone"],
            "notes": r["notes"],
            "created_at": dt
        })
        
    total_pages = (total_count + per_page - 1) // per_page
    
    return render_template(
        "doctor_patients_list.html",
        theme=theme,
        patients=patients,
        q=q,
        sort=sort_by,
        page=page,
        total_pages=total_pages,
        total_count=total_count
    )


@core_bp.route("/doctor/consultations/active", methods=["GET"])
@login_required
def doctor_consultations_active():
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = int(current_user.id)
    t0, t1 = _today_bounds()
    
    consults = db.execute(
        "SELECT id, patient_name, chief_complaint, created_at FROM doctor_consultations WHERE doctor_user_id = ? AND status = 'in_progress' ORDER BY created_at DESC",
        (uid,)
    ).fetchall()
    
    appts = db.execute(
        "SELECT id, patient_name, reason, visit_ts FROM doctor_appointments WHERE doctor_user_id = ? AND visit_ts >= ? AND visit_ts < ? AND status = 'in_progress' ORDER BY visit_ts DESC",
        (uid, t0, t1)
    ).fetchall()
    
    items = []
    for c in consults:
        items.append({
            "type": "consultation",
            "id": c["id"],
            "name": c["patient_name"],
            "reason": c["chief_complaint"] or "General Consultation",
            "started_at": datetime.fromtimestamp(int(c["created_at"])).strftime("%I:%M %p")
        })
    for a in appts:
        items.append({
            "type": "appointment",
            "id": a["id"],
            "name": a["patient_name"],
            "reason": a["reason"] or "Appointment Visit",
            "started_at": datetime.fromtimestamp(int(a["visit_ts"])).strftime("%I:%M %p")
        })
        
    return render_template(
        "doctor_consultations_active.html",
        theme=theme,
        items=items
    )


@core_bp.route("/doctor/consultation/<string:ctype>/<int:cid>/complete", methods=["POST"])
@login_required
def complete_active_consultation(ctype, cid):
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    uid = int(current_user.id)
    duration = int(request.form.get("duration") or 30)
    
    if ctype == "consultation":
        db.execute(
            "UPDATE doctor_consultations SET status = 'completed' WHERE id = ? AND doctor_user_id = ?",
            (cid, uid)
        )
        row = db.execute("SELECT patient_name FROM doctor_consultations WHERE id = ?", (cid,)).fetchone()
        patient_name = row["patient_name"] if row else "Patient"
    else:
        db.execute(
            "UPDATE doctor_appointments SET status = 'completed', duration_minutes = ? WHERE id = ? AND doctor_user_id = ?",
            (duration, cid, uid)
        )
        row = db.execute("SELECT patient_name FROM doctor_appointments WHERE id = ?", (cid,)).fetchone()
        patient_name = row["patient_name"] if row else "Patient"
        
    db.execute(
        "INSERT INTO doctor_activity_logs (doctor_user_id, activity_type, description, created_at) VALUES (?, 'consultation_complete', ?, ?)",
        (uid, f"Completed consultation for {patient_name} (Duration: {duration} mins).", now_ts())
    )
    db.commit()
    flash(f"Consultation with {patient_name} marked as completed.")
    return redirect(url_for("core.doctor_dashboard"))


@core_bp.route("/doctor/payments", methods=["GET"])
@login_required
def doctor_payments():
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = int(current_user.id)
    
    total_revenue = db.execute("SELECT SUM(consultation_fee) AS s FROM doctor_payments WHERE doctor_user_id = ? AND status = 'Paid'", (uid,)).fetchone()["s"] or 0.0
    pending_payments = db.execute("SELECT SUM(consultation_fee) AS s FROM doctor_payments WHERE doctor_user_id = ? AND status = 'Pending'", (uid,)).fetchone()["s"] or 0.0
    paid_payments = total_revenue
    failed_payments = db.execute("SELECT SUM(consultation_fee) AS s FROM doctor_payments WHERE doctor_user_id = ? AND status = 'Failed'", (uid,)).fetchone()["s"] or 0.0
    refunds = db.execute("SELECT SUM(consultation_fee) AS s FROM doctor_payments WHERE doctor_user_id = ? AND status = 'Refunded'", (uid,)).fetchone()["s"] or 0.0
    
    now = datetime.now()
    today_start = int(datetime(now.year, now.month, now.day).timestamp())
    week_start = int((datetime(now.year, now.month, now.day) - timedelta(days=now.weekday())).timestamp())
    month_start = int(datetime(now.year, now.month, 1).timestamp())
    
    today_earnings = db.execute("SELECT SUM(consultation_fee) AS s FROM doctor_payments WHERE doctor_user_id = ? AND status = 'Paid' AND visit_ts >= ?", (uid, today_start)).fetchone()["s"] or 0.0
    weekly_earnings = db.execute("SELECT SUM(consultation_fee) AS s FROM doctor_payments WHERE doctor_user_id = ? AND status = 'Paid' AND visit_ts >= ?", (uid, week_start)).fetchone()["s"] or 0.0
    monthly_earnings = db.execute("SELECT SUM(consultation_fee) AS s FROM doctor_payments WHERE doctor_user_id = ? AND status = 'Paid' AND visit_ts >= ?", (uid, month_start)).fetchone()["s"] or 0.0
    
    q = (request.args.get("q") or "").strip()
    status_filter = (request.args.get("status") or "").strip()
    method_filter = (request.args.get("method") or "").strip()
    
    try:
        page = int(request.args.get("page") or 1)
    except ValueError:
        page = 1
    per_page = 10
    offset = (page - 1) * per_page
    
    sql = "FROM doctor_payments WHERE doctor_user_id = ?"
    params = [uid]
    
    if q:
        sql += " AND patient_name LIKE ?"
        params.append(f"%{q}%")
    if status_filter:
        sql += " AND status = ?"
        params.append(status_filter)
    if method_filter:
        sql += " AND payment_method = ?"
        params.append(method_filter)
        
    total_count = db.execute(f"SELECT COUNT(*) AS c {sql}", tuple(params)).fetchone()["c"]
    
    sql_limit = f"SELECT * {sql} ORDER BY visit_ts DESC LIMIT ? OFFSET ?"
    params.extend([per_page, offset])
    
    rows = db.execute(sql_limit, tuple(params)).fetchall()
    
    payments = []
    for r in rows:
        dt = datetime.fromtimestamp(int(r["visit_ts"])).strftime("%Y-%m-%d %I:%M %p")
        payments.append({
            "id": r["id"],
            "patient_name": r["patient_name"],
            "appointment_id": r["appointment_id"] or "N/A",
            "visit_ts": dt,
            "consultation_fee": r["consultation_fee"],
            "status": r["status"],
            "payment_method": r["payment_method"] or "N/A",
            "transaction_id": r["transaction_id"] or "N/A"
        })
        
    total_pages = (total_count + per_page - 1) // per_page
    
    return render_template(
        "doctor_payments.html",
        theme=theme,
        payments=payments,
        total_revenue=total_revenue,
        pending_payments=pending_payments,
        paid_payments=paid_payments,
        failed_payments=failed_payments,
        refunds=refunds,
        today_earnings=today_earnings,
        weekly_earnings=weekly_earnings,
        monthly_earnings=monthly_earnings,
        q=q,
        status_filter=status_filter,
        method_filter=method_filter,
        page=page,
        total_pages=total_pages,
        total_count=total_count
    )


@core_bp.route("/doctor/payments/export", methods=["GET"])
@login_required
def export_payments_csv():
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    uid = int(current_user.id)
    
    rows = db.execute("SELECT * FROM doctor_payments WHERE doctor_user_id = ? ORDER BY visit_ts DESC", (uid,)).fetchall()
    
    import io
    import csv
    from flask import Response
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Payment ID", "Patient Name", "Appointment ID", "Date/Time", "Consultation Fee", "Status", "Payment Method", "Transaction ID"])
    
    for r in rows:
        dt = datetime.fromtimestamp(int(r["visit_ts"])).strftime("%Y-%m-%d %H:%M")
        writer.writerow([r["id"], r["patient_name"], r["appointment_id"] or "N/A", dt, r["consultation_fee"], r["status"], r["payment_method"] or "N/A", r["transaction_id"] or "N/A"])
        
    response = Response(output.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=payments_report.csv"
    return response


@core_bp.route("/doctor/consultation-time", methods=["GET"])
@login_required
def doctor_consultation_time():
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = int(current_user.id)
    
    avg_row = db.execute("SELECT AVG(duration_minutes) AS val FROM doctor_appointments WHERE doctor_user_id = ? AND status = 'completed'", (uid,)).fetchone()
    avg_duration = round(avg_row["val"], 1) if avg_row and avg_row["val"] else 0.0
    
    max_row = db.execute("SELECT MAX(duration_minutes) AS val FROM doctor_appointments WHERE doctor_user_id = ? AND status = 'completed'", (uid,)).fetchone()
    max_duration = max_row["val"] if max_row and max_row["val"] else 0
    
    min_row = db.execute("SELECT MIN(duration_minutes) AS val FROM doctor_appointments WHERE doctor_user_id = ? AND status = 'completed'", (uid,)).fetchone()
    min_duration = min_row["val"] if min_row and min_row["val"] else 0
    
    now = datetime.now()
    today_start = int(datetime(now.year, now.month, now.day).timestamp())
    week_start = int((datetime(now.year, now.month, now.day) - timedelta(days=now.weekday())).timestamp())
    
    today_row = db.execute("SELECT SUM(duration_minutes) AS val FROM doctor_appointments WHERE doctor_user_id = ? AND status = 'completed' AND visit_ts >= ?", (uid, today_start)).fetchone()
    today_time = today_row["val"] if today_row and today_row["val"] else 0
    
    week_row = db.execute("SELECT SUM(duration_minutes) AS val FROM doctor_appointments WHERE doctor_user_id = ? AND status = 'completed' AND visit_ts >= ?", (uid, week_start)).fetchone()
    weekly_avg = round((week_row["val"] or 0) / 7.0, 1)
    
    chart_labels = []
    chart_data = []
    for i in range(6, -1, -1):
        day_date = now - timedelta(days=i)
        d_start = int(datetime(day_date.year, day_date.month, day_date.day).timestamp())
        d_end = d_start + 86400
        
        day_row = db.execute("SELECT AVG(duration_minutes) AS val FROM doctor_appointments WHERE doctor_user_id = ? AND status = 'completed' AND visit_ts >= ? AND visit_ts < ?", (uid, d_start, d_end)).fetchone()
        chart_labels.append(day_date.strftime("%a"))
        chart_data.append(round(day_row["val"], 1) if day_row and day_row["val"] else 0.0)
        
    return render_template(
        "doctor_consultation_time.html",
        theme=theme,
        avg_duration=avg_duration,
        max_duration=max_duration,
        min_duration=min_duration,
        today_time=today_time,
        weekly_avg=weekly_avg,
        chart_labels=chart_labels,
        chart_data=chart_data
    )


@core_bp.route("/doctor/monthly-patients", methods=["GET"])
@login_required
def doctor_monthly_patients():
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = int(current_user.id)
    
    now = datetime.now()
    month_start = int(datetime(now.year, now.month, 1).timestamp())
    
    new_patients = db.execute("SELECT COUNT(*) FROM doctor_patients WHERE doctor_user_id = ? AND created_at >= ?", (uid, month_start)).fetchone()[0]
    
    ret_patients = db.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT patient_name FROM doctor_appointments
            WHERE doctor_user_id = ? AND visit_ts >= ?
            GROUP BY patient_name HAVING COUNT(*) > 1
        )
        """,
        (uid, month_start)
    ).fetchone()[0]
    
    missed_appts = db.execute("SELECT COUNT(*) FROM doctor_appointments WHERE doctor_user_id = ? AND visit_ts >= ? AND status = 'cancelled'", (uid, month_start)).fetchone()[0]
    completed_consultations = db.execute("SELECT COUNT(*) FROM doctor_appointments WHERE doctor_user_id = ? AND visit_ts >= ? AND status = 'completed'", (uid, month_start)).fetchone()[0]
    
    months: list[str] = []
    y, mo = now.year, now.month
    for _ in range(6):
        months.insert(0, f"{y:04d}-{mo:02d}")
        mo -= 1
        if mo == 0:
            mo = 12
            y -= 1
            
    chart_labels = [datetime(int(m[:4]), int(m[5:7]), 1).strftime("%b %y") for m in months]
    chart_data = []
    for m in months:
        n = db.execute(
            """
            SELECT COUNT(*) FROM doctor_patients
            WHERE doctor_user_id = ?
              AND strftime('%Y-%m', datetime(created_at, 'unixepoch')) = ?
            """,
            (uid, m),
        ).fetchone()[0]
        chart_data.append(n)
        
    return render_template(
        "doctor_monthly_patients.html",
        theme=theme,
        new_patients=new_patients,
        ret_patients=ret_patients,
        missed_appts=missed_appts,
        completed_consultations=completed_consultations,
        chart_labels=chart_labels,
        chart_data=chart_data
    )


@core_bp.get("/health-score")
@login_required
def health_score():
    redir = _require_patient()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = current_user.id
    
    health_score_data = _get_health_score_data(uid)
    
    return render_template(
        "health_score.html",
        theme=theme,
        score=health_score_data["value"],
        category=health_score_data["category"],
        factors=health_score_data["factors"],
        suggestions=health_score_data["suggestions"],
        improvements=health_score_data["improvements"],
        attention=health_score_data["attention"],
        history=health_score_data["history"]
    )


@core_bp.get("/health-trends")
@login_required
def health_trends():
    redir = _require_patient()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = current_user.id
    
    vitals_rows = db.execute(
        """
        SELECT logged_at, bp_systolic, bp_diastolic, sugar, heart_rate, weight
        FROM vitals_logs
        WHERE user_id = ?
        ORDER BY logged_at ASC
        """,
        (uid,),
    ).fetchall()
    
    vitals = [dict(v) for v in vitals_rows]
    for v in vitals:
        if v["logged_at"]:
            try:
                dt = datetime.strptime(v["logged_at"][:16], "%Y-%m-%dT%H:%M")
                v["date_label"] = dt.strftime("%b %d, %Y")
            except ValueError:
                v["date_label"] = v["logged_at"]
        else:
            v["date_label"] = "N/A"
            
    return render_template(
        "health_trends.html",
        theme=theme,
        vitals=vitals
    )


@core_bp.get("/medicine-reminders")
@login_required
def medicine_reminders():
    redir = _require_patient()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = current_user.id
    
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
    today_end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999).strftime("%Y-%m-%dT%H:%M")
    
    reminders_rows = db.execute(
        """
        SELECT id, title, remind_at, is_done, dosage, instructions, med_image, repeat_enabled
        FROM reminders
        WHERE user_id = ? AND reminder_type = 'medication' AND remind_at >= ? AND remind_at <= ?
        ORDER BY remind_at ASC
        """,
        (uid, today_start, today_end),
    ).fetchall()
    
    reminders = [dict(r) for r in reminders_rows]
    now_str = datetime.now().strftime("%Y-%m-%dT%H:%M")
    
    total = len(reminders)
    taken = sum(1 for r in reminders if r["is_done"])
    missed = sum(1 for r in reminders if not r["is_done"] and r["remind_at"] < now_str)
    
    adherence_pct = int((taken / total) * 100) if total > 0 else 0
    
    for r in reminders:
        try:
            dt = datetime.strptime(r["remind_at"][:16], "%Y-%m-%dT%H:%M")
            r["time_fmt"] = dt.strftime("%I:%M %p")
        except ValueError:
            r["time_fmt"] = r["remind_at"]
            
    prescriptions = db.execute(
        "SELECT * FROM prescriptions WHERE user_id = ? ORDER BY id DESC", (uid,)
    ).fetchall()
    prescriptions = [dict(p) for p in prescriptions]
    
    return render_template(
        "medicine_reminders.html",
        theme=theme,
        reminders=reminders,
        prescriptions=prescriptions,
        total=total,
        taken=taken,
        missed=missed,
        adherence_pct=adherence_pct
    )


@core_bp.post("/api/patient/medicine-reminders/<int:reminder_id>/toggle")
@login_required
def toggle_medicine_reminder(reminder_id: int):
    db = get_db()
    r = db.execute("SELECT is_done FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, current_user.id)).fetchone()
    if not r:
        return jsonify({"error": "Reminder not found"}), 404
    
    new_status = 1 if r["is_done"] == 0 else 0
    db.execute("UPDATE reminders SET is_done = ? WHERE id = ? AND user_id = ?", (new_status, reminder_id, current_user.id))
    db.commit()
    return jsonify({"success": True, "is_done": new_status})


@core_bp.get("/health-timeline")
@login_required
def health_timeline():
    redir = _require_patient()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = current_user.id
    
    timeline = []
    
    appt_rows = db.execute(
        """
        SELECT a.appointment_at, a.reason, d.name as doctor_name
        FROM appointments a
        LEFT JOIN doctors d ON a.doctor_id = d.id
        WHERE a.user_id = ?
        """,
        (uid,)
    ).fetchall()
    for row in appt_rows:
        timeline.append({
            "date": row["appointment_at"][:10] if row["appointment_at"] else "",
            "type": "Appointment",
            "title": f"Appointment with {row['doctor_name'] or 'Doctor'}",
            "desc": row["reason"] or "Routine Consultation",
            "icon": "📅",
            "sort_key": row["appointment_at"] or ""
        })
        
    file_rows = db.execute(
        "SELECT filename, category, uploaded_at FROM files WHERE user_id = ?",
        (uid,)
    ).fetchall()
    for row in file_rows:
        dt_str = datetime.fromtimestamp(int(row["uploaded_at"])).strftime("%Y-%m-%d")
        timeline.append({
            "date": dt_str,
            "type": "Medical Report",
            "title": f"Uploaded report: {row['filename']}",
            "desc": f"Category: {row['category'] or 'Uncategorized'}",
            "icon": "📄",
            "sort_key": dt_str + "T00:00"
        })
        
    rx_rows = db.execute(
        "SELECT medicine_name, dosage, frequency, created_at, start_date FROM prescriptions WHERE user_id = ?",
        (uid,)
    ).fetchall()
    for row in rx_rows:
        dt_str = datetime.fromtimestamp(int(row["created_at"])).strftime("%Y-%m-%d")
        timeline.append({
            "date": row["start_date"] or dt_str,
            "type": "Prescription",
            "title": f"Prescribed: {row['medicine_name']} ({row['dosage']})",
            "desc": f"Frequency: {row['frequency']}",
            "icon": "💊",
            "sort_key": (row["start_date"] or dt_str) + "T00:00"
        })
        
    vac_rows = db.execute(
        "SELECT vaccine_name, dose_info, due_date, status FROM vaccinations WHERE user_id = ?",
        (uid,)
    ).fetchall()
    for row in vac_rows:
        timeline.append({
            "date": row["due_date"] or "",
            "type": "Vaccination",
            "title": f"Vaccine: {row['vaccine_name']}",
            "desc": f"Dose: {row['dose_info'] or 'N/A'} - Status: {row['status']}",
            "icon": "💉",
            "sort_key": row["due_date"] or ""
        })
        
    evt_rows = db.execute(
        "SELECT event_date, event_type, description FROM medical_timeline_events WHERE user_id = ?",
        (uid,)
    ).fetchall()
    for row in evt_rows:
        timeline.append({
            "date": row["event_date"] or "",
            "type": "Medical Event",
            "title": f"Event: {row['event_type']}",
            "desc": row["description"],
            "icon": "🩺",
            "sort_key": row["event_date"] or ""
        })
        
    timeline.sort(key=lambda x: x["sort_key"], reverse=True)
    
    return render_template(
        "health_timeline.html",
        theme=theme,
        timeline=timeline
    )


@core_bp.get("/medical-documents")
@login_required
def medical_documents():
    redir = _require_patient()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = current_user.id
    
    search = (request.args.get("q") or "").strip()
    category_filter = (request.args.get("category") or "").strip()
    date_filter = (request.args.get("date_range") or "").strip() # 'today', 'week', 'month', 'year', 'all'
    sort_by = (request.args.get("sort_by") or "newest").strip() # 'newest', 'oldest'
    
    query = "SELECT id, filename, uploaded_at, category, category_confidence, doc_category, doc_source, file_size, file_type, uploaded_by, ai_summary FROM files WHERE user_id = ?"
    params = [uid]
    
    if search:
        query += " AND (filename LIKE ? OR doc_source LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    if category_filter:
        query += " AND category = ?"
        params.append(category_filter)
        
    if date_filter:
        now = datetime.now()
        if date_filter == "today":
            start_ts = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            query += " AND uploaded_at >= ?"
            params.append(start_ts)
        elif date_filter == "week":
            start_ts = int((now - timedelta(days=7)).timestamp())
            query += " AND uploaded_at >= ?"
            params.append(start_ts)
        elif date_filter == "month":
            start_ts = int((now - timedelta(days=30)).timestamp())
            query += " AND uploaded_at >= ?"
            params.append(start_ts)
        elif date_filter == "year":
            start_ts = int((now - timedelta(days=365)).timestamp())
            query += " AND uploaded_at >= ?"
            params.append(start_ts)
            
    if sort_by == "oldest":
        query += " ORDER BY uploaded_at ASC"
    else:
        query += " ORDER BY uploaded_at DESC"
        
    file_rows = db.execute(query, params).fetchall()
    
    files = []
    for f in file_rows:
        d = dict(f)
        d["uploaded_label"] = datetime.fromtimestamp(int(f["uploaded_at"])).strftime("%b %d, %Y")
        sz = f["file_size"]
        if sz:
            if sz < 1024:
                d["size_lbl"] = f"{sz} B"
            elif sz < 1024 * 1024:
                d["size_lbl"] = f"{round(sz / 1024, 1)} KB"
            else:
                d["size_lbl"] = f"{round(sz / (1024 * 1024), 1)} MB"
        else:
            d["size_lbl"] = "Unknown"
        files.append(d)
        
    standard_categories = [
        "Prescriptions", "Blood Test Reports", "X-Ray", "MRI", "CT Scan",
        "ECG", "Ultrasound", "Vaccination Records", "Discharge Summary",
        "Medical Certificates", "Insurance Documents", "Other Medical Documents"
    ]
    categories = []
    for cat in standard_categories:
        count_row = db.execute("SELECT COUNT(*) as c FROM files WHERE user_id = ? AND category = ?", (uid, cat)).fetchone()
        categories.append({
            "category": cat,
            "c": count_row["c"] if count_row else 0
        })
        
    return render_template(
        "medical_documents.html",
        theme=theme,
        files=files,
        categories=categories,
        search=search,
        selected_category=category_filter,
        date_filter=date_filter,
        sort_by=sort_by
    )

@core_bp.get("/api/patient/check-reminders")
@login_required
def check_reminders():
    db = get_db()
    uid = current_user.id
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%dT%H:%M")
    now_ts_val = now_ts()
    
    # 1. Fetch appointments
    ap_rows = db.execute(
        """
        SELECT a.id, a.appointment_at, a.reason, d.name AS doctor_name, d.specialization,
               u.doctor_clinic, dp.hospital_clinic, a.status
        FROM appointments a
        LEFT JOIN doctors d ON a.doctor_id = d.id
        LEFT JOIN users u ON d.user_id = u.id
        LEFT JOIN doctor_profiles dp ON d.user_id = dp.user_id
        WHERE a.user_id = ? AND COALESCE(a.status, '') = 'scheduled'
        ORDER BY a.appointment_at ASC
        """,
        (uid,),
    ).fetchall()
    
    active_appointments = []
    for r in ap_rows:
        at = r["appointment_at"] or ""
        try:
            if "T" in at:
                apt_dt = datetime.strptime(at[:16], "%Y-%m-%dT%H:%M")
            else:
                apt_dt = datetime.strptime(at[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
            
        time_diff = apt_dt - now
        minutes_diff = int(time_diff.total_seconds() / 60)
        
        clinic_name = r["hospital_clinic"] or r["doctor_clinic"] or "MedVault Clinic"
        reason = r["reason"] or "General Consultation"
        low = reason.lower()
        consultation_type = "Online" if ("video" in low or "tele" in low or "online" in low) else "Offline"
        
        if -30 <= minutes_diff <= 1500:
            dn = (r["doctor_name"] or "").strip() or "Your provider"
            doc_display = dn if dn.lower().startswith("dr") else f"Dr. {dn}"
            
            active_appointments.append({
                "id": r["id"],
                "doctor_name": doc_display,
                "specialization": r["specialization"] or "General Practitioner",
                "appointment_at": at,
                "date_fmt": apt_dt.strftime("%b %d, %Y"),
                "time_fmt": apt_dt.strftime("%I:%M %p"),
                "minutes_diff": minutes_diff,
                "clinic_name": clinic_name,
                "consultation_type": consultation_type,
                "reason": reason,
                "timestamp": int(apt_dt.timestamp())
            })
            
    # 2. Fetch ignored and active medicines
    fifteen_mins_ago = now - timedelta(minutes=15)
    fifteen_mins_ago_str = fifteen_mins_ago.strftime("%Y-%m-%dT%H:%M")
    
    ignored_reminders = db.execute(
        """
        SELECT id, title, remind_at, snooze_until, follow_up_sent, repeat_enabled
        FROM reminders
        WHERE user_id = ? AND reminder_type = 'medication' AND is_done = 0
        AND (
            (snooze_until IS NULL AND remind_at <= ?)
            OR (snooze_until IS NOT NULL AND snooze_until <= ? AND snooze_until != 'EXPIRED')
        )
        """,
        (uid, fifteen_mins_ago_str, fifteen_mins_ago_str)
    ).fetchall()
    
    for rem in ignored_reminders:
        if rem["repeat_enabled"] == 1:
            new_snooze = (datetime.now() + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M")
            db.execute("UPDATE reminders SET snooze_until = ? WHERE id = ?", (new_snooze, rem["id"]))
            db.execute(
                """
                INSERT INTO patient_notifications (user_id, category, title, message, is_read, created_at)
                VALUES (?, 'Medicines', ?, ?, 0, ?)
                """,
                (uid, "Medication Nudge", f"Follow-up: Time to take your medicine {rem['title']}.", now_ts_val)
            )
        elif rem["follow_up_sent"] == 0:
            new_snooze = (datetime.now() + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M")
            db.execute("UPDATE reminders SET snooze_until = ?, follow_up_sent = 1 WHERE id = ?", (new_snooze, rem["id"]))
            db.execute(
                """
                INSERT INTO patient_notifications (user_id, category, title, message, is_read, created_at)
                VALUES (?, 'Medicines', ?, ?, 0, ?)
                """,
                (uid, "Medication Follow-up", f"Follow-up: Remember to take {rem['title']}.", now_ts_val)
            )
        else:
            db.execute("UPDATE reminders SET snooze_until = 'EXPIRED' WHERE id = ?", (rem["id"],))
            
    db.commit()
    
    med_rows = db.execute(
        """
        SELECT id, title, remind_at, dosage, instructions, med_image, snooze_until, repeat_enabled
        FROM reminders
        WHERE user_id = ? AND reminder_type = 'medication' AND is_done = 0
        AND (
            (snooze_until IS NULL AND remind_at <= ?)
            OR (snooze_until IS NOT NULL AND snooze_until <= ? AND snooze_until != 'EXPIRED')
        )
        ORDER BY remind_at ASC
        """,
        (uid, now_str, now_str)
    ).fetchall()
    
    active_medicines = []
    for r in med_rows:
        active_medicines.append({
            "id": r["id"],
            "title": r["title"],
            "remind_at": r["remind_at"],
            "dosage": r["dosage"] or "1 dose",
            "instructions": r["instructions"] or "After Food",
            "med_image": r["med_image"] or "",
            "snooze_until": r["snooze_until"],
            "repeat_enabled": r["repeat_enabled"]
        })
        
    unread_count_row = db.execute(
        "SELECT COUNT(*) as count FROM patient_notifications WHERE user_id = ? AND is_read = 0",
        (uid,)
    ).fetchone()
    unread_count = unread_count_row["count"] if unread_count_row else 0
    
    return jsonify({
        "appointments": active_appointments,
        "medicines": active_medicines,
        "unread_count": unread_count
    })

@core_bp.post("/api/patient/medicine-reminders/<int:reminder_id>/taken")
@login_required
def medicine_reminder_taken(reminder_id: int):
    db = get_db()
    uid = current_user.id
    r = db.execute(
        "SELECT id, title, dosage, instructions FROM reminders WHERE id = ? AND user_id = ?",
        (reminder_id, uid)
    ).fetchone()
    if not r:
        return jsonify({"error": "Reminder not found"}), 404
        
    now_val = now_ts()
    db.execute("UPDATE reminders SET is_done = 1 WHERE id = ?", (reminder_id,))
    db.execute(
        """
        INSERT INTO medicine_history (user_id, reminder_id, medicine_name, dosage, taken_at, instructions, status)
        VALUES (?, ?, ?, ?, ?, ?, 'taken')
        """,
        (uid, reminder_id, r["title"], r["dosage"] or "1 dose", now_val, r["instructions"] or "After Food")
    )
    db.execute(
        """
        INSERT INTO patient_notifications (user_id, category, title, message, is_read, created_at)
        VALUES (?, 'Medicines', 'Medicine Taken', ?, 0, ?)
        """,
        (uid, f"You marked '{r['title']}' ({r['dosage'] or '1 dose'}) as taken.", now_val)
    )
    db.commit()
    return jsonify({"success": True})

@core_bp.post("/api/patient/medicine-reminders/<int:reminder_id>/skip")
@login_required
def medicine_reminder_skip(reminder_id: int):
    db = get_db()
    uid = current_user.id
    r = db.execute(
        "SELECT id, title, dosage, instructions FROM reminders WHERE id = ? AND user_id = ?",
        (reminder_id, uid)
    ).fetchone()
    if not r:
        return jsonify({"error": "Reminder not found"}), 404
        
    now_val = now_ts()
    db.execute("UPDATE reminders SET is_done = 1, snooze_until = 'EXPIRED' WHERE id = ?", (reminder_id,))
    db.execute(
        """
        INSERT INTO medicine_history (user_id, reminder_id, medicine_name, dosage, taken_at, instructions, status)
        VALUES (?, ?, ?, ?, ?, ?, 'skipped')
        """,
        (uid, reminder_id, r["title"], r["dosage"] or "1 dose", now_val, r["instructions"] or "After Food")
    )
    db.execute(
        """
        INSERT INTO patient_notifications (user_id, category, title, message, is_read, created_at)
        VALUES (?, 'Medicines', 'Medicine Skipped', ?, 0, ?)
        """,
        (uid, f"You skipped medicine '{r['title']}'.", now_val)
    )
    db.commit()
    return jsonify({"success": True})

@core_bp.post("/api/patient/medicine-reminders/<int:reminder_id>/snooze")
@login_required
def medicine_reminder_snooze(reminder_id: int):
    snooze_minutes = int(request.form.get("minutes", 10))
    db = get_db()
    uid = current_user.id
    r = db.execute("SELECT id FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, uid)).fetchone()
    if not r:
        return jsonify({"error": "Reminder not found"}), 404
        
    snooze_time = (datetime.now() + timedelta(minutes=snooze_minutes)).strftime("%Y-%m-%dT%H:%M")
    db.execute("UPDATE reminders SET snooze_until = ? WHERE id = ?", (snooze_time, reminder_id))
    db.commit()
    return jsonify({"success": True, "snooze_until": snooze_time})

@core_bp.route("/notifications", methods=["GET"])
@login_required
def notifications_page():
    redir = _require_patient()
    if redir:
        return redir
    db = get_db()
    uid = current_user.id
    theme = _portal_theme(uid, current_user.username)
    
    category = request.args.get("category", "").strip()
    search = request.args.get("q", "").strip()
    
    query = "SELECT * FROM patient_notifications WHERE user_id = ?"
    params = [uid]
    
    if category:
        query += " AND category = ?"
        params.append(category)
        
    if search:
        query += " AND (title LIKE ? OR message LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
        
    query += " ORDER BY created_at DESC"
    notif_rows = db.execute(query, params).fetchall()
    
    notifications = []
    for r in notif_rows:
        d = dict(r)
        d["created_label"] = datetime.fromtimestamp(int(r["created_at"])).strftime("%b %d, %Y %I:%M %p")
        notifications.append(d)
        
    counts = {}
    categories_list = ["Appointments", "Medicines", "Reports", "Prescriptions", "Payments", "Doctor Messages", "General"]
    for cat in categories_list:
        row = db.execute("SELECT COUNT(*) as c FROM patient_notifications WHERE user_id = ? AND category = ?", (uid, cat)).fetchone()
        counts[cat] = row["c"] if row else 0
        
    return render_template(
        "notifications.html",
        theme=theme,
        notifications=notifications,
        counts=counts,
        selected_category=category,
        search=search
    )

@core_bp.post("/api/patient/notifications/<int:notif_id>/read")
@login_required
def read_notification(notif_id: int):
    db = get_db()
    db.execute("UPDATE patient_notifications SET is_read = 1 WHERE id = ? AND user_id = ?", (notif_id, current_user.id))
    db.commit()
    return jsonify({"success": True})

@core_bp.post("/api/patient/notifications/read-all")
@login_required
def read_all_notifications():
    db = get_db()
    db.execute("UPDATE patient_notifications SET is_read = 1 WHERE user_id = ?", (current_user.id,))
    db.commit()
    return jsonify({"success": True})

@core_bp.post("/api/patient/notifications/<int:notif_id>/delete")
@login_required
def delete_notification(notif_id: int):
    db = get_db()
    db.execute("DELETE FROM patient_notifications WHERE id = ? AND user_id = ?", (notif_id, current_user.id))
    db.commit()
    return jsonify({"success": True})


@core_bp.get("/favorite-doctors")
@login_required
def favorite_doctors():
    redir = _require_patient()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = current_user.id
    
    doctors_rows = db.execute(
        "SELECT * FROM doctors WHERE user_id = ? ORDER BY name ASC",
        (uid,)
    ).fetchall()
    doctors = [dict(d) for d in doctors_rows]
    
    return render_template(
        "favorite_doctors.html",
        theme=theme,
        doctors=doctors
    )


@core_bp.get("/emergency-info")
@login_required
def emergency_info():
    redir = _require_patient()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = current_user.id
    
    prof = db.execute("SELECT * FROM patient_profiles WHERE user_id = ?", (uid,)).fetchone()
    prof = dict(prof) if prof else {}
    
    user_row = db.execute("SELECT full_name, blood_group, allergies FROM users WHERE id = ?", (uid,)).fetchone()
    
    full_name = decrypt_text(user_row["full_name"]) if user_row else ""
    blood_group = decrypt_text(user_row["blood_group"]) if user_row else prof.get("blood_group", "")
    allergies = decrypt_text(user_row["allergies"]) if user_row else prof.get("allergies", "")
    
    emergency_contact_name = prof.get("emergency_contact_name", "")
    emergency_contact_number = prof.get("emergency_contact_number", "")
    
    token_row = db.execute(
        "SELECT token, expiry_time FROM emergency_tokens WHERE user_id = ? ORDER BY expiry_time DESC LIMIT 1",
        (uid,)
    ).fetchone()
    
    token = None
    expiry_time = None
    if token_row:
        if token_row["expiry_time"] > int(time.time()):
            token = token_row["token"]
            expiry_time = datetime.fromtimestamp(token_row["expiry_time"], tz=timezone.utc).isoformat()
            
    return render_template(
        "emergency_info.html",
        theme=theme,
        full_name=full_name,
        blood_group=blood_group,
        allergies=allergies,
        emergency_contact_name=emergency_contact_name,
        emergency_contact_number=emergency_contact_number,
        token=token,
        expires_at=expiry_time
    )


@core_bp.get("/api/patient/notifications")
@login_required
def api_patient_notifications():
    db = get_db()
    uid = current_user.id
    
    ap_rows = db.execute(
        """
        SELECT a.id, a.appointment_at, a.reason, d.name AS doctor_name, a.status
        FROM appointments a
        LEFT JOIN doctors d ON a.doctor_id = d.id
        WHERE a.user_id = ? AND COALESCE(a.status, '') = 'scheduled'
        ORDER BY a.appointment_at ASC
        """,
        (uid,),
    ).fetchall()
    
    now = datetime.now()
    notifications = []
    
    for r in ap_rows:
        at = r["appointment_at"] or ""
        try:
            if "T" in at:
                apt_dt = datetime.strptime(at[:16], "%Y-%m-%dT%H:%M")
            else:
                apt_dt = datetime.strptime(at[:10], "%Y-%m-%d")
        except ValueError:
            continue
            
        time_diff = apt_dt - now
        minutes_diff = int(time_diff.total_seconds() / 60)
        
        if -30 <= minutes_diff <= 60:
            dn = (r["doctor_name"] or "").strip() or "Your provider"
            doc_display = dn if dn.lower().startswith("dr") else f"Dr. {dn}"
            join_appropriate = (-30 <= minutes_diff <= 15)
            
            notifications.append({
                "appointment_id": r["id"],
                "doctor_name": doc_display,
                "appointment_at": at,
                "when_fmt": _fmt_appt_display(at),
                "minutes_diff": minutes_diff,
                "join_appropriate": join_appropriate,
                "reason": r["reason"] or "Consultation",
                "message": f"Appointment with {doc_display} starts in {minutes_diff} minutes!" if minutes_diff > 0 else f"Appointment with {doc_display} starting now!"
            })
            
    return jsonify({
        "notifications": notifications,
        "badge_count": len(notifications)
    })


def _get_vitals_analytics(uid):
    db = get_db()
    now = datetime.now()
    seven_days_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    thirty_days_ago = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    
    # Query weekly logs
    weekly_logs = db.execute(
        """
        SELECT bp_systolic, bp_diastolic, sugar, heart_rate, bmi, sleep_hours, water_intake, logged_at
        FROM vitals_logs
        WHERE user_id = ? AND logged_at >= ?
        ORDER BY logged_at ASC
        """,
        (uid, seven_days_ago)
    ).fetchall()
    
    # Query monthly logs
    monthly_logs = db.execute(
        """
        SELECT bp_systolic, bp_diastolic, sugar, heart_rate, bmi, sleep_hours, water_intake, logged_at
        FROM vitals_logs
        WHERE user_id = ? AND logged_at >= ?
        ORDER BY logged_at ASC
        """,
        (uid, thirty_days_ago)
    ).fetchall()
    
    def calc_stats(logs):
        sys_vals = [r["bp_systolic"] for r in logs if r["bp_systolic"] is not None]
        dia_vals = [r["bp_diastolic"] for r in logs if r["bp_diastolic"] is not None]
        sugar_vals = [r["sugar"] for r in logs if r["sugar"] is not None]
        hr_vals = [r["heart_rate"] for r in logs if r["heart_rate"] is not None]
        bmi_vals = [r["bmi"] for r in logs if r["bmi"] is not None]
        sleep_vals = [r["sleep_hours"] for r in logs if r["sleep_hours"] is not None]
        water_vals = [r["water_intake"] for r in logs if r["water_intake"] is not None]
        
        return {
            "bp_max": f"{max(sys_vals)}/{max(dia_vals)}" if sys_vals and dia_vals else "N/A",
            "bp_min": f"{min(sys_vals)}/{min(dia_vals)}" if sys_vals and dia_vals else "N/A",
            "sugar_max": f"{max(sugar_vals):g}" if sugar_vals else "N/A",
            "sugar_min": f"{min(sugar_vals):g}" if sugar_vals else "N/A",
            "hr_max": max(hr_vals) if hr_vals else "N/A",
            "hr_min": min(hr_vals) if hr_vals else "N/A",
            "bmi_avg": round(sum(bmi_vals) / len(bmi_vals), 1) if bmi_vals else "N/A",
            "sleep_avg": round(sum(sleep_vals) / len(sleep_vals), 1) if sleep_vals else "N/A",
            "water_avg": round(sum(water_vals) / len(water_vals), 1) if water_vals else "N/A"
        }
        
    weekly_stats = calc_stats(weekly_logs)
    monthly_stats = calc_stats(monthly_logs)
    
    # Highlights:
    lowest_bp_log = None
    min_bp_sys = 9999
    highest_sugar_log = None
    max_sugar = -1.0
    highest_risk_log = None
    max_risk_score = -1
    best_log = None
    min_deviation = 9999
    
    for r in monthly_logs:
        if r["bp_systolic"] is not None:
            if r["bp_systolic"] < min_bp_sys:
                min_bp_sys = r["bp_systolic"]
                lowest_bp_log = r
                
        if r["sugar"] is not None:
            if r["sugar"] > max_sugar:
                max_sugar = r["sugar"]
                highest_sugar_log = r
                
        # Risk score
        sys_v = r["bp_systolic"] or 120
        hr_v = r["heart_rate"] or 72
        risk_score = sys_v + hr_v
        if risk_score > max_risk_score:
            max_risk_score = risk_score
            highest_risk_log = r
            
        # Best day deviation
        if r["bp_systolic"] is not None and r["sugar"] is not None:
            dev = abs(r["bp_systolic"] - 120) + abs(r["bp_diastolic"] - 80) + abs(r["sugar"] - 90)
            if dev < min_deviation:
                min_deviation = dev
                best_log = r
                
    def fmt_day(log):
        if not log or not log["logged_at"]:
            return "N/A"
        try:
            dt = datetime.strptime(log["logged_at"][:10], "%Y-%m-%d")
            return dt.strftime("%b %d, %Y")
        except Exception:
            return log["logged_at"][:10]
            
    highlights = {
        "best_day": fmt_day(best_log),
        "highest_risk_day": fmt_day(highest_risk_log),
        "lowest_bp_day": fmt_day(lowest_bp_log),
        "highest_sugar_day": fmt_day(highest_sugar_log)
    }
    
    # Health Trend status
    h_data = _get_health_score_data(uid)
    score_val = h_data["value"]
    
    last_log = db.execute("SELECT logged_at FROM vitals_logs WHERE user_id = ? ORDER BY logged_at DESC LIMIT 1", (uid,)).fetchone()
    vitals_not_updated_7_days = False
    if last_log and last_log["logged_at"]:
        try:
            dt = datetime.strptime(last_log["logged_at"][:16], "%Y-%m-%dT%H:%M")
            if (datetime.now() - dt).days >= 7:
                vitals_not_updated_7_days = True
        except Exception:
            pass
    else:
        vitals_not_updated_7_days = True
        
    if vitals_not_updated_7_days:
        trend = "Needs Attention"
    elif score_val >= 80:
        trend = "Improving"
    elif score_val >= 60:
        trend = "Stable"
    else:
        trend = "Needs Attention"
        
    highlights["trend"] = trend
    
    return {
        "weekly": weekly_stats,
        "monthly": monthly_stats,
        "highlights": highlights
    }


def _generate_vitals_ai_insights(uid):
    db = get_db()
    logs = db.execute(
        "SELECT bp_systolic, bp_diastolic, sugar, heart_rate, water_intake, logged_at FROM vitals_logs WHERE user_id = ? ORDER BY logged_at DESC LIMIT 10",
        (uid,)
    ).fetchall()
    
    insights = []
    if not logs:
        insights.append({
            "icon": "🤖",
            "title": "Welcome to AI Insights",
            "desc": "Start logging your daily vitals to generate dynamic health recommendations and lifestyle tips.",
            "priority": "info"
        })
        return insights
        
    latest = logs[0]
    
    # BP comparison
    if len(logs) > 1 and latest["bp_systolic"]:
        prev_bp = [r["bp_systolic"] for r in logs[1:] if r["bp_systolic"]]
        if prev_bp:
            avg_prev = sum(prev_bp) / len(prev_bp)
            if latest["bp_systolic"] < avg_prev - 5:
                insights.append({
                    "icon": "📈",
                    "title": "Blood Pressure Improvement",
                    "desc": "Your systolic blood pressure has improved compared to your historical average.",
                    "priority": "high"
                })
            elif latest["bp_systolic"] > avg_prev + 5:
                insights.append({
                    "icon": "⚠️",
                    "title": "Elevated Blood Pressure",
                    "desc": "Your systolic reading is slightly higher than your usual average. Keep tracking and minimize salt intake.",
                    "priority": "high"
                })
                
    # Heart rate stability
    if len(logs) > 3 and latest["heart_rate"]:
        hr_vals = [r["heart_rate"] for r in logs if r["heart_rate"]]
        avg_hr = sum(hr_vals) / len(hr_vals)
        variance = sum((x - avg_hr) ** 2 for x in hr_vals) / len(hr_vals)
        if variance < 25:
            insights.append({
                "icon": "❤️",
                "title": "Heart Rate Stability",
                "desc": "Your resting heart rate has remained stable within your normal baseline.",
                "priority": "medium"
            })
            
    # Blood sugar trend
    if len(logs) > 2 and latest["sugar"]:
        sugar_vals = [r["sugar"] for r in logs[:3] if r["sugar"]]
        if len(sugar_vals) == 3 and sugar_vals[0] > sugar_vals[1] > sugar_vals[2]:
            insights.append({
                "icon": "🍬",
                "title": "Blood Sugar Alert",
                "desc": "Your blood sugar levels show a gradual increase over the last three readings.",
                "priority": "high"
            })
        elif latest["sugar"] < 100:
            insights.append({
                "icon": "🥗",
                "title": "Optimal Blood Sugar",
                "desc": "Your latest blood sugar reading is in the optimal fasting range.",
                "priority": "medium"
            })
            
    # Hydration level
    if latest["water_intake"]:
        if latest["water_intake"] < 2.0:
            insights.append({
                "icon": "💧",
                "title": "Low Hydration Level",
                "desc": f"Your logged water intake ({latest['water_intake']}L) is below the recommended daily target of 2.5L.",
                "priority": "medium"
            })
        else:
            insights.append({
                "icon": "🥤",
                "title": "Excellent Hydration",
                "desc": "Great job! You met or exceeded your daily hydration goal of 2.0 liters today.",
                "priority": "info"
            })
            
    # Check routine checkup
    last_appt = db.execute("SELECT appointment_at FROM appointments WHERE user_id = ? ORDER BY appointment_at DESC LIMIT 1", (uid,)).fetchone()
    if not last_appt:
        insights.append({
            "icon": "🩺",
            "title": "Routine Screening Suggestion",
            "desc": "Consider scheduling a routine wellness consultation with a general practitioner.",
            "priority": "info"
        })
        
    return insights


@core_bp.route("/health-vitals", methods=["GET", "POST"])
@login_required
def health_vitals():
    redir = _require_patient()
    if redir:
        return redir
        
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = current_user.id
    
    # Check onboarding info
    prof = db.execute("SELECT height, weight FROM patient_profiles WHERE user_id = ?", (uid,)).fetchone()
    has_height = prof and prof["height"] > 0
    has_weight = prof and prof["weight"] > 0
    
    if request.method == "POST":
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json
        
        if request.is_json:
            data = request.json
        else:
            data = request.form
            
        logged_at = data.get("logged_at")
        bp_sys = data.get("bp_systolic")
        bp_dia = data.get("bp_diastolic")
        sugar = data.get("sugar")
        heart_rate = data.get("heart_rate")
        spo2 = data.get("spo2")
        temp = data.get("temperature")
        bmi_val = data.get("bmi")
        chol = data.get("cholesterol")
        water = data.get("water_intake")
        steps = data.get("steps")
        sleep = data.get("sleep_hours")
        notes = data.get("notes")
        update_flag = data.get("update") == "true" or data.get("update") is True
        
        def to_num(val, val_type=float):
            if val is not None:
                if isinstance(val, (int, float)):
                    return val_type(val)
                if isinstance(val, str) and val.strip() != "":
                    try:
                        return val_type(val)
                    except ValueError:
                        return None
            return None

        if not logged_at:
            logged_at = datetime.now().strftime("%Y-%m-%dT%H:%M")
            
        date_str = logged_at[:10]
        
        # Check if an entry for this day already exists for this user
        existing_log = db.execute(
            "SELECT id FROM vitals_logs WHERE user_id = ? AND substr(logged_at, 1, 10) = ?",
            (uid, date_str)
        ).fetchone()
        
        if existing_log and not update_flag:
            if is_ajax:
                return jsonify({
                    "status": "duplicate",
                    "message": "Vitals for today have already been logged. Do you want to update the existing entry?"
                })
            else:
                flash("Vitals for today have already been logged.")
                return redirect(url_for("core.health_vitals"))
                
        if existing_log and update_flag:
            db.execute(
                """
                UPDATE vitals_logs SET
                    logged_at = ?, bp_systolic = ?, bp_diastolic = ?, sugar = ?, heart_rate = ?,
                    spo2 = ?, temperature = ?, bmi = ?, cholesterol = ?, water_intake = ?, steps = ?, sleep_hours = ?,
                    symptoms = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    logged_at,
                    to_num(bp_sys, int),
                    to_num(bp_dia, int),
                    to_num(sugar, float),
                    to_num(heart_rate, int),
                    to_num(spo2, int),
                    to_num(temp, float),
                    to_num(bmi_val, float),
                    to_num(chol, float),
                    to_num(water, float),
                    to_num(steps, int),
                    to_num(sleep, float),
                    notes or "",
                    existing_log["id"],
                    uid
                )
            )
        else:
            db.execute(
                """
                INSERT INTO vitals_logs (
                    user_id, logged_at, bp_systolic, bp_diastolic, sugar, heart_rate,
                    spo2, temperature, bmi, cholesterol, water_intake, steps, sleep_hours,
                    symptoms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid,
                    logged_at,
                    to_num(bp_sys, int),
                    to_num(bp_dia, int),
                    to_num(sugar, float),
                    to_num(heart_rate, int),
                    to_num(spo2, int),
                    to_num(temp, float),
                    to_num(bmi_val, float),
                    to_num(chol, float),
                    to_num(water, float),
                    to_num(steps, int),
                    to_num(sleep, float),
                    notes or "",
                    now_ts()
                )
            )
        db.commit()
        
        if is_ajax:
            return jsonify({
                "status": "success",
                "message": "Today's health vitals have been saved successfully."
            })
        else:
            flash("Vitals reading logged successfully.")
            return redirect(url_for("core.health_vitals"))
        
    # GET: fetch previous readings
    vitals_rows = db.execute(
        """
        SELECT * FROM vitals_logs 
        WHERE user_id = ? 
        ORDER BY logged_at DESC, id DESC
        """,
        (uid,)
    ).fetchall()
    
    vitals = []
    for r in vitals_rows:
        v = dict(r)
        if v["logged_at"]:
            try:
                dt = datetime.strptime(v["logged_at"][:16], "%Y-%m-%dT%H:%M")
                v["date_label"] = dt.strftime("%b %d, %Y at %I:%M %p")
            except ValueError:
                v["date_label"] = v["logged_at"]
        else:
            v["date_label"] = "N/A"
        vitals.append(v)
        
    analytics = _get_vitals_analytics(uid)
    ai_insights = _generate_vitals_ai_insights(uid)
    
    # Check if logged today
    today_str = datetime.now().strftime("%Y-%m-%d")
    logged_today = any(v["logged_at"] and v["logged_at"].startswith(today_str) for v in vitals)
        
    return render_template(
        "health_vitals.html",
        theme=theme,
        vitals=vitals,
        has_height=has_height,
        has_weight=has_weight,
        analytics=analytics,
        ai_insights=ai_insights,
        logged_today=logged_today
    )


# ==========================================
# DOCTOR PORTAL REDESIGN ADDITIONS
# ==========================================

def _generate_practice_insights(db, uid: int) -> list[dict]:
    # Calculate stats
    total_distinct = db.execute("SELECT COUNT(DISTINCT patient_name) FROM doctor_appointments WHERE doctor_user_id = ?", (uid,)).fetchone()[0] or 0
    completed_tot = db.execute("SELECT COUNT(*) FROM doctor_appointments WHERE doctor_user_id = ? AND status = 'completed'", (uid,)).fetchone()[0] or 0
    avg_dur = db.execute("SELECT AVG(duration_minutes) FROM doctor_appointments WHERE doctor_user_id = ? AND status = 'completed'", (uid,)).fetchone()[0] or 18
    cancelled_tot = db.execute("SELECT COUNT(*) FROM doctor_appointments WHERE doctor_user_id = ? AND status = 'cancelled'", (uid,)).fetchone()[0] or 0
    total_appt = db.execute("SELECT COUNT(*) FROM doctor_appointments WHERE doctor_user_id = ?", (uid,)).fetchone()[0] or 1
    cancellation_rate = round((cancelled_tot / total_appt) * 100, 1)
    
    # Most common disease from consultations
    common_complaint = "Hypertension"
    disease_rows = db.execute("SELECT chief_complaint FROM doctor_consultations WHERE doctor_user_id = ?", (uid,)).fetchall()
    if disease_rows:
        from collections import Counter
        words = []
        for r in disease_rows:
            complaint = (r["chief_complaint"] or "").lower()
            if "cough" in complaint or "sob" in complaint or "breath" in complaint or "respiratory" in complaint or "lung" in complaint or "asthma" in complaint:
                words.append("Respiratory Infections")
            elif "bp" in complaint or "pressure" in complaint or "hypertension" in complaint or "htn" in complaint:
                words.append("Hypertension")
            elif "diabetes" in complaint or "glucose" in complaint or "sugar" in complaint or "hba1c" in complaint:
                words.append("Diabetes Mellitus")
            elif complaint.strip():
                words.append("General Health")
        if words:
            common_complaint = Counter(words).most_common(1)[0][0]
            
    insights = [
        {"title": "Patient Growth Trend", "text": "Your clinic registry has grown, showing positive patient acquisition.", "metric": f"+{total_distinct} Total Patients", "icon": "📈"},
        {"title": "Consultation Duration", "text": f"Your average patient consultation duration is {int(avg_dur)} minutes.", "metric": f"{int(avg_dur)} min avg", "icon": "⏱️"},
        {"title": "Common Diagnoses", "text": f"Most common patient chief complaint is related to {common_complaint}.", "metric": common_complaint, "icon": "🩺"},
        {"title": "Cancellation Rate", "text": f"Your appointment cancellation rate is {cancellation_rate}%.", "metric": f"{cancellation_rate}% rate", "icon": "🗓️"},
        {"title": "Follow-up Compliance", "text": "Ensure that pending follow-ups are assigned for chronic care patients.", "metric": "Action Recommended", "icon": "📝"}
    ]
    return insights


def _get_patient_details_by_name(db, name: str) -> dict:
    p = db.execute(
        "SELECT id, username, email, phone, gender, dob, allergies, medications, conditions FROM users WHERE full_name = ? AND portal_role = 'patient' LIMIT 1",
        (name,)
    ).fetchone()
    if p:
        p_id = p["id"]
        profile = db.execute("SELECT * FROM patient_profiles WHERE user_id = ?", (p_id,)).fetchone()
        blood_group = profile["blood_group"] if profile else "Not Specified"
        organ_donor = profile["organ_donor"] if (profile and "organ_donor" in profile.keys()) else "Not Specified"
        emergency_contact = profile["emergency_contact"] if profile else "N/A"
        
        last_appt = db.execute(
            "SELECT visit_ts FROM doctor_appointments WHERE patient_name = ? AND status = 'completed' ORDER BY visit_ts DESC LIMIT 1",
            (name,)
        ).fetchone()
        last_visit = datetime.fromtimestamp(last_appt["visit_ts"]).strftime("%b %d, %Y") if last_appt else "No past visits"
        
        files_rows = db.execute(
            "SELECT id, filename, category, created_at FROM medical_documents WHERE user_id = ? ORDER BY created_at DESC",
            (p_id,)
        ).fetchall()
        files = []
        for f in files_rows:
            files.append({
                "id": f["id"],
                "filename": f["filename"],
                "category": f["category"] or "Other",
                "created_at": datetime.fromtimestamp(int(f["created_at"])).strftime("%b %d, %Y") if str(f["created_at"]).isdigit() else str(f["created_at"])
            })
        
        consults_count = db.execute(
            "SELECT COUNT(*) FROM doctor_consultations WHERE patient_name = ?",
            (name,)
        ).fetchone()[0]
        
        presc_rows = db.execute(
            "SELECT id, medicine_name, dosage, frequency, created_at FROM doctor_prescriptions WHERE patient_name = ? ORDER BY created_at DESC",
            (name,)
        ).fetchall()
        prescriptions = []
        for pr in presc_rows:
            prescriptions.append({
                "id": pr["id"],
                "medicine_name": pr["medicine_name"],
                "dosage": pr["dosage"],
                "frequency": pr["frequency"],
                "created_at": datetime.fromtimestamp(int(pr["created_at"])).strftime("%b %d, %Y") if str(pr["created_at"]).isdigit() else str(pr["created_at"])
            })

        vitals_rows = db.execute(
            "SELECT * FROM vitals_logs WHERE user_id = ? ORDER BY logged_at DESC LIMIT 5",
            (p_id,)
        ).fetchall()
        vitals = []
        for v in vitals_rows:
            vitals.append({
                "logged_at": v["logged_at"],
                "bp_systolic": v["bp_systolic"],
                "bp_diastolic": v["bp_diastolic"],
                "sugar": v["sugar"],
                "heart_rate": v["heart_rate"],
                "symptoms": v["symptoms"] or "None"
            })
        
        return {
            "matched": True,
            "user_id": p_id,
            "email": p["email"],
            "phone": p["phone"] or "N/A",
            "gender": p["gender"] or "Not Specified",
            "dob": p["dob"] or "N/A",
            "allergies": p["allergies"] or "None",
            "medications": p["medications"] or "None",
            "conditions": p["conditions"] or "None",
            "blood_group": blood_group,
            "organ_donor": organ_donor,
            "emergency_contact": emergency_contact,
            "last_visit": last_visit,
            "files": files,
            "consults_count": consults_count,
            "prescriptions": prescriptions,
            "vitals": vitals,
        }
    return {
        "matched": False,
        "email": "N/A",
        "phone": "N/A",
        "gender": "Not Specified",
        "dob": "N/A",
        "allergies": "None",
        "medications": "None",
        "conditions": "None",
        "blood_group": "Not Specified",
        "organ_donor": "Not Specified",
        "emergency_contact": "N/A",
        "last_visit": "No past visits",
        "files": [],
        "consults_count": 0,
        "prescriptions": [],
        "vitals": [],
    }


@core_bp.route("/doctor/patient/<int:patient_db_id>/details", methods=["GET"])
@login_required
def get_patient_slider_details(patient_db_id: int):
    redir = _require_doctor()
    if redir:
        return jsonify({"error": "Forbidden"}), 403
    db = get_db()
    row = db.execute("SELECT full_name, risk_level, notes FROM doctor_patients WHERE id = ? AND doctor_user_id = ?", (patient_db_id, current_user.id)).fetchone()
    if not row:
        return jsonify({"error": "Patient not found"}), 404
        
    details = _get_patient_details_by_name(db, row["full_name"])
    details["patient_db_id"] = patient_db_id
    details["notes"] = row["notes"] or ""
    details["risk_level"] = row["risk_level"] or "Low"
    return jsonify(details)


@core_bp.route("/doctor/patient/<int:patient_db_id>/update-clinical", methods=["POST"])
@login_required
def update_patient_clinical(patient_db_id: int):
    redir = _require_doctor()
    if redir:
        return redirect(url_for("core.dashboard"))
    db = get_db()
    risk_level = request.form.get("risk_level", "Low")
    notes = request.form.get("notes", "").strip()
    db.execute(
        "UPDATE doctor_patients SET risk_level = ?, notes = ? WHERE id = ? AND doctor_user_id = ?",
        (risk_level, notes, patient_db_id, current_user.id)
    )
    db.commit()
    flash("Patient clinical record updated.")
    return redirect(url_for("core.doctor_patients"))


@core_bp.route("/doctor/consultation/workspace", methods=["GET", "POST"])
@login_required
def doctor_consultation_workspace():
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = current_user.id
    
    patient_id = request.args.get("patient_id") # doctor_patients ID
    selected_patient = None
    patient_data = None
    
    patients = db.execute(
        "SELECT id, full_name, risk_level FROM doctor_patients WHERE doctor_user_id = ? ORDER BY full_name ASC",
        (uid,)
    ).fetchall()
    
    if patient_id:
        selected_patient = db.execute(
            "SELECT * FROM doctor_patients WHERE id = ? AND doctor_user_id = ?",
            (patient_id, uid)
        ).fetchone()
        if selected_patient:
            patient_data = _get_patient_details_by_name(db, selected_patient["full_name"])
            
    templates = db.execute(
        "SELECT * FROM prescription_templates WHERE doctor_user_id = ? ORDER BY template_name ASC",
        (uid,)
    ).fetchall()
    
    if request.method == "POST":
        p_name = request.form.get("patient_name", "").strip()
        complaint = request.form.get("chief_complaint", "").strip()
        notes = request.form.get("notes", "").strip()
        risk = request.form.get("risk_level", "Low")
        
        v_bp = request.form.get("vitals_bp", "").strip()
        v_hr = request.form.get("vitals_hr", "").strip()
        v_temp = request.form.get("vitals_temp", "").strip()
        v_spo2 = request.form.get("vitals_spo2", "").strip()
        diag = request.form.get("diagnoses", "").strip()
        f_up = request.form.get("follow_up_date", "").strip()
        presc = request.form.get("prescription_text", "").strip()
        
        if not p_name or not complaint:
            flash("Patient name and Chief Complaint are required.")
            return redirect(url_for("core.doctor_consultation_workspace", patient_id=patient_id))
            
        # Write consultation record
        db.execute(
            """
            INSERT INTO doctor_consultations 
            (doctor_user_id, patient_name, chief_complaint, notes, status, created_at,
             vitals_bp, vitals_hr, vitals_temp, vitals_spo2, diagnoses, follow_up_date, prescription_text, risk_level)
            VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (uid, p_name, complaint, notes, now_ts(), v_bp, v_hr, v_temp, v_spo2, diag, f_up, presc, risk)
        )
        
        # If there is a linked patient account, add vitals & alerts to their profile
        p_user = db.execute(
            "SELECT id FROM users WHERE full_name = ? AND portal_role = 'patient' LIMIT 1",
            (p_name,)
        ).fetchone()
        
        if p_user:
            # Insert Vitals Log
            systolic, diastolic = 120, 80
            if "/" in v_bp:
                try:
                    parts = v_bp.split("/")
                    systolic = int(parts[0])
                    diastolic = int(parts[1])
                except ValueError:
                    pass
            hr = 72
            if v_hr.isdigit():
                hr = int(v_hr)
            
            db.execute(
                """
                INSERT INTO vitals_logs (user_id, logged_at, bp_systolic, bp_diastolic, sugar, heart_rate, symptoms)
                VALUES (?, ?, ?, ?, 110.0, ?, ?)
                """,
                (p_user["id"], datetime.now().strftime("%Y-%m-%dT%H:%M"), systolic, diastolic, hr, complaint)
            )
            
            # If prescription text provided, add to doctor_prescriptions
            if presc:
                db.execute(
                    """
                    INSERT INTO doctor_prescriptions (doctor_user_id, patient_name, medicine_name, dosage, frequency, notes, duration_days, sent_to_patient, created_at)
                    VALUES (?, ?, ?, 'As Directed', 'Standard', ?, 7, 1, ?)
                    """,
                    (uid, p_name, presc[:100], presc, now_ts())
                )
        
        # If follow-up date, add reminder
        if f_up:
            db.execute(
                """
                INSERT INTO doctor_reminders (doctor_user_id, patient_name, title, remind_at, is_done, created_at)
                VALUES (?, ?, ?, ?, 0, ?)
                """,
                (uid, p_name, f"Follow-up for {complaint}", f_up, now_ts())
            )
            
        # Update risk_level on doctor_patients
        db.execute(
            "UPDATE doctor_patients SET risk_level = ?, last_visit = ? WHERE full_name = ? AND doctor_user_id = ?",
            (risk, datetime.now().strftime("%b %d, %Y"), p_name, uid)
        )
        
        db.execute(
            "INSERT INTO doctor_activity_logs (doctor_user_id, activity_type, description, created_at) VALUES (?, ?, ?, ?)",
            (uid, "consultation_add", f"Completed consultation for {p_name}.", now_ts())
        )
        db.commit()
        flash(f"Consultation completed successfully for {p_name}.")
        return redirect(url_for("core.doctor_dashboard"))
        
    return render_template(
        "doctor_consultation_workspace.html",
        theme=theme,
        patients=patients,
        selected_patient=selected_patient,
        patient_data=patient_data,
        templates=templates,
    )


@core_bp.route("/doctor/notifications", methods=["GET", "POST"])
@login_required
def doctor_notifications_list():
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = current_user.id
    
    if request.method == "POST":
        # Mark all as read
        db.execute("DELETE FROM doctor_smart_alerts WHERE doctor_user_id = ?", (uid,))
        db.commit()
        flash("All alerts dismissed.")
        return redirect(url_for("core.doctor_notifications_list"))
        
    alerts = db.execute("SELECT * FROM doctor_smart_alerts WHERE doctor_user_id = ? ORDER BY created_at DESC", (uid,)).fetchall()
    return render_template("doctor_notifications.html", theme=theme, alerts=alerts)


@core_bp.route("/doctor/documents", methods=["GET"])
@login_required
def doctor_documents_center():
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = current_user.id
    
    q = (request.args.get("q") or "").strip()
    category = (request.args.get("category") or "").strip()
    
    # We load all reports for patients linked to this doctor
    linked_names = [r["full_name"] for r in db.execute("SELECT full_name FROM doctor_patients WHERE doctor_user_id = ?", (uid,)).fetchall()]
    
    docs = []
    if linked_names:
        placeholders = ",".join("?" for _ in linked_names)
        user_ids = [r["id"] for r in db.execute(f"SELECT id FROM users WHERE full_name IN ({placeholders}) AND portal_role = 'patient'", tuple(linked_names)).fetchall()]
        
        if user_ids:
            doc_placeholders = ",".join("?" for _ in user_ids)
            sql = f"SELECT * FROM medical_documents WHERE user_id IN ({doc_placeholders})"
            params = list(user_ids)
            
            if q:
                sql += " AND filename LIKE ?"
                params.append(f"%{q}%")
            if category:
                sql += " AND category = ?"
                params.append(category)
                
            sql += " ORDER BY created_at DESC"
            rows = db.execute(sql, tuple(params)).fetchall()
            for r in rows:
                owner = db.execute("SELECT full_name FROM users WHERE id = ?", (r["user_id"],)).fetchone()
                docs.append({
                    "id": r["id"],
                    "filename": r["filename"],
                    "category": r["category"] or "Other",
                    "patient_name": owner["full_name"] if owner else "Unknown",
                    "created_at": datetime.fromtimestamp(int(r["created_at"])).strftime("%b %d, %Y") if str(r["created_at"]).isdigit() else str(r["created_at"])
                })
                
    categories = ["Blood Report", "X-Ray", "MRI", "CT Scan", "ECG", "Ultrasound", "Prescription", "Vaccination Record", "Discharge Summary", "Other"]
    return render_template("doctor_documents.html", theme=theme, docs=docs, q=q, category=category, categories=categories)


@core_bp.route("/doctor/performance", methods=["GET"])
@login_required
def doctor_performance_dashboard():
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = current_user.id
    
    payload = _analytics_payload(db, uid)
    
    # Calculate performance ratios
    total_distinct = db.execute("SELECT COUNT(DISTINCT patient_name) FROM doctor_appointments WHERE doctor_user_id = ?", (uid,)).fetchone()[0] or 0
    total_completed = db.execute("SELECT COUNT(*) FROM doctor_appointments WHERE doctor_user_id = ? AND status = 'completed'", (uid,)).fetchone()[0] or 0
    total_cancelled = db.execute("SELECT COUNT(*) FROM doctor_appointments WHERE doctor_user_id = ? AND status = 'cancelled'", (uid,)).fetchone()[0] or 0
    total_appt = db.execute("SELECT COUNT(*) FROM doctor_appointments WHERE doctor_user_id = ?", (uid,)).fetchone()[0] or 1
    cancellation_rate = round((total_cancelled / total_appt) * 100, 1)
    
    return render_template(
        "doctor_performance.html",
        theme=theme,
        chart=payload,
        total_distinct=total_distinct,
        total_completed=total_completed,
        total_cancelled=total_cancelled,
        cancellation_rate=cancellation_rate,
    )


@core_bp.route("/patient/profile", methods=["GET", "POST"])
@login_required
def patient_profile():
    if _portal_role(current_user.id) != "patient":
        flash("This page is for patient accounts only.")
        return redirect(url_for("core.dashboard"))
        
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    
    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        profile_photo = (request.form.get("profile_photo") or "").strip()
        try:
            age = int(request.form.get("age") or 0)
        except ValueError:
            age = 0
        gender = (request.form.get("gender") or "").strip()
        dob = (request.form.get("dob") or "").strip()
        blood_group = (request.form.get("blood_group") or "").strip()
        try:
            height = float(request.form.get("height") or 0.0)
        except ValueError:
            height = 0.0
        try:
            weight = float(request.form.get("weight") or 0.0)
        except ValueError:
            weight = 0.0
            
        phone = (request.form.get("phone") or "").strip()
        email = (request.form.get("email") or "").strip()
        address = (request.form.get("address") or "").strip()
        emergency_contact_name = (request.form.get("emergency_contact_name") or "").strip()
        emergency_contact_number = (request.form.get("emergency_contact_number") or "").strip()
        
        existing_diseases = (request.form.get("existing_diseases") or "").strip()
        current_medications = (request.form.get("current_medications") or "").strip()
        allergies = (request.form.get("allergies") or "").strip()
        previous_surgeries = (request.form.get("previous_surgeries") or "").strip()
        family_medical_history = (request.form.get("family_medical_history") or "").strip()
        smoking_status = (request.form.get("smoking_status") or "").strip()
        alcohol_consumption = (request.form.get("alcohol_consumption") or "").strip()
        
        exercise_frequency = (request.form.get("exercise_frequency") or "").strip()
        try:
            sleep_duration = float(request.form.get("sleep_duration") or 0.0)
        except ValueError:
            sleep_duration = 0.0
        diet_preference = (request.form.get("diet_preference") or "").strip()
        try:
            water_intake = float(request.form.get("water_intake") or 0.0)
        except ValueError:
            water_intake = 0.0
        occupation = (request.form.get("occupation") or "").strip()
        preferred_language = (request.form.get("preferred_language") or "").strip()
        preferred_consultation_mode = (request.form.get("preferred_consultation_mode") or "").strip()
        
        errors = []
        if not full_name:
            errors.append("Full name is required.")
        if age <= 0:
            errors.append("Valid age is required.")
        if not dob:
            errors.append("Date of birth is required.")
        if not phone:
            errors.append("Phone number is required.")
        if not email:
            errors.append("Email address is required.")
        if not emergency_contact_name or not emergency_contact_number:
            errors.append("Emergency contact details are required.")
        if height < 0:
            errors.append("Height cannot be negative.")
        if weight < 0:
            errors.append("Weight cannot be negative.")
            
        if errors:
            for error in errors:
                flash(error)
        else:
            try:
                db.execute(
                    """
                    UPDATE users 
                    SET full_name = ?, blood_group = ?, allergies = ?, medications = ?, conditions = ?
                    WHERE id = ?
                    """,
                    (
                        encrypt_text(full_name),
                        encrypt_text(blood_group),
                        encrypt_text(allergies),
                        encrypt_text(current_medications),
                        encrypt_text(existing_diseases),
                        current_user.id
                    )
                )
                
                row = db.execute("SELECT 1 FROM patient_profiles WHERE user_id = ?", (current_user.id,)).fetchone()
                if row:
                    db.execute(
                        """
                        UPDATE patient_profiles SET
                          full_name=?, profile_photo=?, age=?, gender=?, dob=?, blood_group=?, height=?, weight=?,
                          phone=?, email=?, address=?, emergency_contact_name=?, emergency_contact_number=?,
                          existing_diseases=?, current_medications=?, allergies=?, previous_surgeries=?,
                          family_medical_history=?, smoking_status=?, alcohol_consumption=?, exercise_frequency=?,
                          sleep_duration=?, diet_preference=?, water_intake=?, occupation=?, preferred_language=?,
                          preferred_consultation_mode=?
                        WHERE user_id = ?
                        """,
                        (
                            full_name, profile_photo, age, gender, dob, blood_group, height, weight,
                            phone, email, address, emergency_contact_name, emergency_contact_number,
                            existing_diseases, current_medications, allergies, previous_surgeries,
                            family_medical_history, smoking_status, alcohol_consumption, exercise_frequency,
                            sleep_duration, diet_preference, water_intake, occupation, preferred_language,
                            preferred_consultation_mode, current_user.id
                        )
                    )
                else:
                    db.execute(
                        """
                        INSERT INTO patient_profiles (
                          user_id, full_name, profile_photo, age, gender, dob, blood_group, height, weight,
                          phone, email, address, emergency_contact_name, emergency_contact_number,
                          existing_diseases, current_medications, allergies, previous_surgeries,
                          family_medical_history, smoking_status, alcohol_consumption, exercise_frequency,
                          sleep_duration, diet_preference, water_intake, occupation, preferred_language,
                          preferred_consultation_mode
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            current_user.id, full_name, profile_photo, age, gender, dob, blood_group, height, weight,
                            phone, email, address, emergency_contact_name, emergency_contact_number,
                            existing_diseases, current_medications, allergies, previous_surgeries,
                            family_medical_history, smoking_status, alcohol_consumption, exercise_frequency,
                            sleep_duration, diet_preference, water_intake, occupation, preferred_language,
                            preferred_consultation_mode
                        )
                    )
                db.commit()
                flash("Medical profile updated securely.")
            except Exception as e:
                db.rollback()
                current_app.logger.error(f"Error updating patient profile: {e}", exc_info=True)
                flash(f"A database error occurred: {str(e)}")
                
    profile = db.execute("SELECT * FROM patient_profiles WHERE user_id = ?", (current_user.id,)).fetchone()
    if not profile:
        dec_name = ""
        dec_email = ""
        dec_phone = ""
        dec_allergies = ""
        dec_meds = ""
        dec_conds = ""
        try:
            dec_name = decrypt_text(current_user.full_name) or ""
            dec_email = decrypt_text(current_user.email) or ""
            dec_phone = decrypt_text(current_user.phone) or ""
            dec_allergies = decrypt_text(current_user.allergies) or ""
            dec_meds = decrypt_text(current_user.medications) or ""
            dec_conds = decrypt_text(current_user.conditions) or ""
        except Exception:
            pass
            
        profile = {
            "full_name": dec_name or current_user.username,
            "profile_photo": "",
            "age": 0,
            "gender": "Other",
            "dob": "",
            "blood_group": "",
            "height": 0.0,
            "weight": 0.0,
            "phone": dec_phone,
            "email": dec_email,
            "address": "",
            "emergency_contact_name": "",
            "emergency_contact_number": "",
            "existing_diseases": dec_conds,
            "current_medications": dec_meds,
            "allergies": dec_allergies,
            "previous_surgeries": "",
            "family_medical_history": "",
            "smoking_status": "No",
            "alcohol_consumption": "No",
            "exercise_frequency": "None",
            "sleep_duration": 0.0,
            "diet_preference": "",
            "water_intake": 0.0,
            "occupation": "",
            "preferred_language": "English",
            "preferred_consultation_mode": "In-Person",
        }
        
    return render_template("patient_profile.html", theme=theme, profile=profile)


@core_bp.route("/patient/payments", methods=["GET"])
@login_required
def patient_payments():
    if _portal_role(current_user.id) != "patient":
        flash("This page is for patient accounts only.")
        return redirect(url_for("core.dashboard"))
        
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    
    patient_full_name = ""
    try:
        patient_full_name = decrypt_text(current_user.full_name) or ""
    except Exception:
        pass
        
    payments_raw = db.execute(
        """
        SELECT 
            p.id, 
            p.consultation_fee, 
            p.status, 
            p.payment_method, 
            p.transaction_id, 
            p.visit_ts,
            p.created_at,
            d.full_name as doctor_name,
            d.username as doctor_username,
            a.appointment_at
        FROM doctor_payments p
        LEFT JOIN users d ON p.doctor_user_id = d.id
        LEFT JOIN appointments a ON p.appointment_id = a.id
        WHERE a.user_id = ? OR p.patient_name = ? OR p.patient_name = ?
        ORDER BY p.created_at DESC
        """,
        (current_user.id, current_user.username, patient_full_name),
    ).fetchall()
    
    payments = []
    total_paid = 0.0
    total_pending = 0.0
    
    for row in payments_raw:
        doc_name = row["doctor_name"] or row["doctor_username"] or "Unknown Doctor"
        if not (doc_name.startswith("Dr.") or doc_name.startswith("dr.")):
            try:
                dec_doc = decrypt_text(row["doctor_name"]) or ""
                if dec_doc:
                    doc_name = dec_doc
            except Exception:
                pass
            if not doc_name.lower().startswith("dr"):
                doc_name = f"Dr. {doc_name}"
                
        date_str = ""
        if row["visit_ts"]:
            try:
                date_str = datetime.fromtimestamp(row["visit_ts"]).strftime("%Y-%m-%d %I:%M %p")
            except Exception:
                pass
        if not date_str and row["appointment_at"]:
            date_str = row["appointment_at"]
            
        fee = row["consultation_fee"] or 0.0
        status = row["status"] or "Pending"
        
        if status == "Paid":
            total_paid += fee
        elif status == "Pending":
            total_pending += fee
            
        payments.append({
            "id": row["id"],
            "doctor_name": doc_name,
            "fee": fee,
            "status": status,
            "method": row["payment_method"] or "N/A",
            "transaction_id": row["transaction_id"] or "N/A",
            "date": date_str,
        })
        
    return render_template(
        "patient_payments.html",
        theme=theme,
        payments=payments,
        total_paid=total_paid,
        total_pending=total_pending
    )


@core_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    theme = _portal_theme(current_user.id, current_user.username)
    db = get_db()
    
    if request.method == "POST":
        current_password = request.form.get("current_password")
        new_password = request.form.get("new_password")
        confirm_password = request.form.get("confirm_password")
        
        if not current_password or not new_password or not confirm_password:
            flash("All password fields are required.")
        elif new_password != confirm_password:
            flash("New passwords do not match.")
        elif len(new_password) < 6:
            flash("New password must be at least 6 characters long.")
        else:
            row = db.execute("SELECT password_hash FROM users WHERE id = ?", (current_user.id,)).fetchone()
            if row and verify_password(current_password, row["password_hash"])[0]:
                new_hash = hash_password(new_password)
                db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, current_user.id))
                db.commit()
                flash("Password changed successfully.")
                if theme["portal_role"] == "doctor":
                    return redirect(url_for("core.doctor_dashboard"))
                else:
                    return redirect(url_for("core.dashboard"))
            else:
                flash("Incorrect current password.")
                
    return render_template("change_password.html", theme=theme)


@core_bp.get("/patient/appointments")
@login_required
def patient_appointments():
    redir = _require_patient()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = current_user.id
    
    rows = db.execute(
        """
        SELECT a.id, a.appointment_at, a.reason, a.status, d.name as doctor_name, d.specialization
        FROM appointments a
        LEFT JOIN doctors d ON a.doctor_id = d.id
        WHERE a.user_id = ?
        ORDER BY a.appointment_at DESC
        """,
        (uid,)
    ).fetchall()
    
    appointments_list = []
    for r in rows:
        dt_str = r["appointment_at"]
        dt = None
        is_upcoming = False
        if dt_str:
            try:
                # Handle YYYY-MM-DD HH:MM
                dt = datetime.strptime(dt_str.strip(), "%Y-%m-%d %H:%M")
                is_upcoming = dt.timestamp() >= time.time()
            except ValueError:
                try:
                    # Handle YYYY-MM-DD
                    dt = datetime.strptime(dt_str.strip()[:10], "%Y-%m-%d")
                    is_upcoming = dt.timestamp() >= time.time()
                except ValueError:
                    pass
        
        appointments_list.append({
            "id": r["id"],
            "appointment_at": dt_str,
            "reason": r["reason"] or "General Consultation",
            "status": r["status"],
            "doctor_name": r["doctor_name"] or "Unknown Provider",
            "specialization": r["specialization"] or "General Practitioner",
            "is_upcoming": is_upcoming
        })
        
    upcoming = [a for a in appointments_list if a["is_upcoming"] and a["status"] == "scheduled"]
    past = [a for a in appointments_list if (not a["is_upcoming"] and a["status"] == "scheduled") or a["status"] == "completed"]
    cancelled = [a for a in appointments_list if a["status"] == "cancelled"]
    
    doctors = db.execute(
        "SELECT id, name, specialization FROM doctors WHERE user_id = ? ORDER BY name",
        (uid,)
    ).fetchall()
    
    return render_template(
        "patient_appointments.html",
        theme=theme,
        upcoming=upcoming,
        past=past,
        cancelled=cancelled,
        doctors=doctors,
        today=datetime.now().strftime("%Y-%m-%d")
    )


@core_bp.get("/health-goals")
@login_required
def health_goals():
    redir = _require_patient()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = current_user.id
    
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_row = db.execute(
        """
        SELECT *
        FROM vitals_logs
        WHERE user_id = ? AND substr(logged_at, 1, 10) = ?
        """,
        (uid, today_str)
    ).fetchone()
    
    today_log = dict(today_row) if today_row else {}
    
    prof = db.execute(
        """
        SELECT water_intake as water_target, sleep_duration as sleep_target
        FROM patient_profiles
        WHERE user_id = ?
        """,
        (uid,)
    ).fetchone()
    
    water_logged = today_log.get("water_intake") if today_log.get("water_intake") is not None else 0.0
    steps_logged = today_log.get("steps") if today_log.get("steps") is not None else 0
    sleep_logged = today_log.get("sleep_hours") if today_log.get("sleep_hours") is not None else 0.0
    
    water_target = prof["water_target"] if prof and prof["water_target"] else 2.5
    sleep_target = prof["sleep_target"] if prof and prof["sleep_target"] else 8.0
    steps_target = 10000
    
    water_pct = min(int((water_logged / water_target) * 100), 100) if water_target > 0 else 0
    sleep_pct = min(int((sleep_logged / sleep_target) * 100), 100) if sleep_target > 0 else 0
    steps_pct = min(int((steps_logged / steps_target) * 100), 100)
    
    overall_progress = int((water_pct + sleep_pct + steps_pct) / 3)
    
    # We can also get latest logs for history list
    recent_logs = db.execute(
        """
        SELECT logged_at, water_intake, steps, sleep_hours
        FROM vitals_logs
        WHERE user_id = ?
        ORDER BY logged_at DESC
        LIMIT 7
        """,
        (uid,)
    ).fetchall()
    
    history = [dict(r) for r in recent_logs]
    
    return render_template(
        "health_goals.html",
        theme=theme,
        water_logged=water_logged,
        water_target=water_target,
        water_pct=water_pct,
        steps_logged=steps_logged,
        steps_target=steps_target,
        steps_pct=steps_pct,
        sleep_logged=sleep_logged,
        sleep_target=sleep_target,
        sleep_pct=sleep_pct,
        overall_progress=overall_progress,
        today_log=today_log,
        history=history,
        logged_today=bool(today_log)
    )






