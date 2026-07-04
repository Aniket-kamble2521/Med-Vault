import os
import re
import secrets
import time
from datetime import datetime, timedelta

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_from_directory, url_for
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from app_pkg.db import get_db
from app_pkg.services.doctor_ai import suggest_from_symptoms
from app_pkg.services.files import ai_categorize_medical_file
from app_pkg.services.ocr import extract_text_with_tesseract
from app_pkg.services.security import decrypt_text, encrypt_text
from app_pkg.services.summary import generate_medical_summary
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
    specialty = (row["doctor_specialty"] or "").strip() if row else ""
    if portal == "doctor" and not specialty:
        specialty = "Cardiologist"
    if portal == "patient" and not specialty:
        specialty = "Patient portal"
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
    }


def _redirect_home():
    if _portal_role(current_user.id) == "doctor":
        return redirect(url_for("core.doctor_dashboard"))
    return redirect(url_for("core.dashboard"))


def _require_doctor():
    if _portal_role(current_user.id) != "doctor":
        flash("That area is for doctors only.")
        return redirect(url_for("core.dashboard"))
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
        ("Sarah Johnson", "Routine Checkup", base + 10 * 3600, "waiting"),
        ("Michael Chen", "Follow-up", base + 11 * 3600 + 30 * 60, "in_progress"),
        ("Emily Davis", "Lab Review", base + 14 * 3600, "scheduled"),
    ]
    ts = now_ts()
    for name, reason, vts, status in demos:
        db.execute(
            """
            INSERT INTO doctor_appointments
            (doctor_user_id, patient_name, reason, visit_ts, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (doctor_id, name, reason, vts, status, ts),
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
    """Charts: patient trends, disease buckets, recovery-style index from visit data."""
    uid = doctor_id
    now = datetime.now()
    months: list[str] = []
    y, mo = now.year, now.month
    for _ in range(6):
        months.insert(0, f"{y:04d}-{mo:02d}")
        mo -= 1
        if mo == 0:
            mo = 12
            y -= 1

    trend_labels = [datetime(int(m[:4]), int(m[5:7]), 1).strftime("%b %y") for m in months]
    trend_values: list[int] = []
    for m in months:
        n = db.execute(
            """
            SELECT COUNT(*) AS c FROM doctor_patients
            WHERE doctor_user_id = ?
              AND strftime('%Y-%m', datetime(created_at, 'unixepoch')) = ?
            """,
            (uid, m),
        ).fetchone()["c"]
        trend_values.append(int(n))

    if sum(trend_values) == 0:
        trend_values = [2, 3, 2, 4, 3, max(1, int(db.execute(
            "SELECT COUNT(*) AS c FROM doctor_patients WHERE doctor_user_id = ?",
            (uid,),
        ).fetchone()["c"] or 0))]

    buckets = {"Hypertension": 0, "Diabetes": 0, "Respiratory": 0, "Other": 0}
    for row in db.execute(
        """
        SELECT chief_complaint FROM doctor_consultations WHERE doctor_user_id = ?
        """,
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

    recovery_labels = trend_labels[:]
    recovery_values: list[float] = []
    for i, m in enumerate(months):
        total_ap = db.execute(
            """
            SELECT COUNT(*) AS c FROM doctor_appointments
            WHERE doctor_user_id = ?
              AND strftime('%Y-%m', datetime(visit_ts, 'unixepoch')) = ?
            """,
            (uid, m),
        ).fetchone()["c"]
        done_like = db.execute(
            """
            SELECT COUNT(*) AS c FROM doctor_appointments
            WHERE doctor_user_id = ?
              AND strftime('%Y-%m', datetime(visit_ts, 'unixepoch')) = ?
              AND status IN ('waiting', 'in_progress')
            """,
            (uid, m),
        ).fetchone()["c"]
        base = 82.0
        if total_ap:
            rate = min(99.0, base + 12.0 * (1.0 - min(1.0, done_like / max(total_ap, 1))))
        else:
            rate = base + i * 0.8
        recovery_values.append(round(rate, 1))
    if all(v == recovery_values[0] for v in recovery_values):
        recovery_values = [88 + i * 1.2 for i in range(6)]

    return {
        "trend_labels": trend_labels,
        "trend_values": trend_values,
        "disease_labels": disease_labels,
        "disease_values": disease_values,
        "recovery_labels": recovery_labels,
        "recovery_values": recovery_values,
        "total_patients": db.execute(
            "SELECT COUNT(*) AS c FROM doctor_patients WHERE doctor_user_id = ?",
            (uid,),
        ).fetchone()["c"],
        "total_consults": db.execute(
            "SELECT COUNT(*) AS c FROM doctor_consultations WHERE doctor_user_id = ?",
            (uid,),
        ).fetchone()["c"],
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
        if int(u["onboarding_done"] or 0) == 0:
            return redirect(url_for("core.onboarding"))
        if (u["portal_role"] or "patient").strip().lower() == "doctor":
            return redirect(url_for("core.doctor_dashboard"))
        return redirect(url_for("core.dashboard"))
    return render_template("index.html")


@core_bp.get("/dashboard")
@login_required
def dashboard():
    if _portal_role(current_user.id) == "doctor":
        return redirect(url_for("core.doctor_dashboard"))
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
    }
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
        bp_display = "120/80 mmHg"
    hr_display = int(vit["heart_rate"]) if vit and vit["heart_rate"] is not None else 72
    if vit and vit["weight"] is not None:
        weight_lbs = round(float(vit["weight"]) * 2.20462, 1)
    else:
        weight_lbs = 165.0
    temp_display = "98.6 °F"

    vitals_cards = [
        {"label": "Blood pressure", "value": bp_display, "status": "Normal", "tone": "pink"},
        {"label": "Heart rate", "value": f"{hr_display} bpm", "status": "Normal", "tone": "teal"},
        {"label": "Temperature", "value": temp_display, "status": "Normal", "tone": "orange"},
        {"label": "Weight", "value": f"{weight_lbs:g} lbs", "status": "Normal", "tone": "purple"},
    ]

    ap_rows = db.execute(
        """
        SELECT a.appointment_at, a.reason, a.status, d.name AS doctor_name, d.specialization
        FROM appointments a
        LEFT JOIN doctors d ON a.doctor_id = d.id
        WHERE a.user_id = ? AND IFNULL(a.status, '') != 'cancelled'
        ORDER BY a.appointment_at ASC
        """,
        (uid,),
    ).fetchall()
    now = datetime.now()
    upcoming_items: list[dict] = []
    for r in ap_rows:
        at = r["appointment_at"] or ""
        try:
            if "T" in at:
                apt_dt = datetime.strptime(at[:16], "%Y-%m-%dT%H:%M")
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
        badge = "Follow-up" if "follow" in low else "Routine checkup"
        upcoming_items.append(
            {
                "doctor_name": doc_display,
                "specialization": (r["specialization"] or "General practice").strip(),
                "reason": reason,
                "badge": badge,
                "when_fmt": _fmt_appt_display(at),
                "initials": _doctor_initials(dn.replace("Dr.", "").replace("dr.", "").strip()),
                "demo": False,
            }
        )
        if len(upcoming_items) >= 4:
            break

    if not upcoming_items:
        upcoming_items = [
            {
                "doctor_name": "Dr. Amanda Smith",
                "specialization": "Cardiology",
                "reason": "Annual review",
                "badge": "Follow-up",
                "when_fmt": "Apr 5, 2026 · 10:00 AM",
                "initials": "AS",
                "demo": True,
            },
            {
                "doctor_name": "Dr. Michael Chen",
                "specialization": "General physician",
                "reason": "Routine visit",
                "badge": "Routine checkup",
                "when_fmt": "Apr 12, 2026 · 2:30 PM",
                "initials": "MC",
                "demo": True,
            },
        ]

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
    if not rx_list:
        rx_list = [
            {"name": "Lisinopril", "dose": "10mg", "freq": "Once daily", "refill": "Apr 15, 2026", "demo": True},
            {"name": "Metformin", "dose": "500mg", "freq": "Twice daily", "refill": "Apr 20, 2026", "demo": True},
        ]

    score = 72
    if profile["full_name"]:
        score += 6
    if profile["medications"]:
        score += 4
    if profile["conditions"]:
        score += 4
    if files:
        score += min(14, len(files) * 2)
    score = min(95, max(62, score))
    health_score = {"value": score, "label": "Excellent health" if score >= 82 else "Good progress"}

    greeting_name = _patient_greeting_name(profile["full_name"], profile["username"])
    first_appt = upcoming_items[0] if upcoming_items else None

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
    )


@core_bp.get("/doctor")
@login_required
def doctor_dashboard():
    if _portal_role(current_user.id) != "doctor":
        return redirect(url_for("core.dashboard"))
    db = get_db()
    _seed_demo_appointments(db, current_user.id)
    db.commit()

    theme = _portal_theme(current_user.id, current_user.username)
    uid = current_user.id
    t0, t1 = _today_bounds()

    total_patients = db.execute(
        "SELECT COUNT(*) AS c FROM doctor_patients WHERE doctor_user_id = ?",
        (uid,),
    ).fetchone()["c"]
    appt_today_n = db.execute(
        """
        SELECT COUNT(*) AS c FROM doctor_appointments
        WHERE doctor_user_id = ? AND visit_ts >= ? AND visit_ts < ?
        """,
        (uid, t0, t1),
    ).fetchone()["c"]
    waiting_n = db.execute(
        """
        SELECT COUNT(*) AS c FROM doctor_appointments
        WHERE doctor_user_id = ? AND visit_ts >= ? AND visit_ts < ? AND status = 'waiting'
        """,
        (uid, t0, t1),
    ).fetchone()["c"]
    active_n = db.execute(
        "SELECT COUNT(*) AS c FROM doctor_consultations WHERE doctor_user_id = ? AND status = 'in_progress'",
        (uid,),
    ).fetchone()["c"]
    active_n += db.execute(
        """
        SELECT COUNT(*) AS c FROM doctor_appointments
        WHERE doctor_user_id = ? AND visit_ts >= ? AND visit_ts < ? AND status = 'in_progress'
        """,
        (uid, t0, t1),
    ).fetchone()["c"]

    kpis = [
        {"label": "Total Patients", "value": str(total_patients), "delta": "", "tone": "teal", "up": True},
        {"label": "Appointments Today", "value": str(appt_today_n), "delta": "", "tone": "blue", "up": True},
        {"label": "Waiting now", "value": str(waiting_n), "delta": "", "tone": "pink", "up": waiting_n > 0},
        {"label": "Active consults", "value": str(active_n), "delta": "", "tone": "red", "up": active_n > 0},
    ]

    q = (request.args.get("q") or "").strip()
    sql = """
        SELECT patient_name, reason, visit_ts, status
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
                "initials": _doctor_initials(row["patient_name"]),
                "name": row["patient_name"],
                "sub": (row["reason"] or "").strip() or "Visit",
                "time": _fmt_visit_time(int(row["visit_ts"])),
                "status": st if st in ("waiting", "in_progress", "scheduled") else "scheduled",
            }
        )

    notifications = _doctor_notifications(db, uid)

    month_start = int(datetime(datetime.now().year, datetime.now().month, 1).timestamp())
    month_treated = db.execute(
        """
        SELECT COUNT(*) AS c FROM doctor_patients
        WHERE doctor_user_id = ? AND created_at >= ?
        """,
        (uid, month_start),
    ).fetchone()["c"]

    return render_template(
        "doctor_dashboard.html",
        theme=theme,
        kpis=kpis,
        appointments=appointments,
        notifications=notifications,
        appt_search=q,
        appt_has_query=bool(q),
        month_treated=month_treated,
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
    row = db.execute(
        """
        SELECT full_name, doctor_specialty, doctor_phone, doctor_clinic, doctor_bio
        FROM users WHERE id = ?
        """,
        (current_user.id,),
    ).fetchone()
    if not row:
        flash("Account not found.")
        return redirect(url_for("auth.logout"))

    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        specialty = (request.form.get("doctor_specialty") or "").strip()[:120]
        phone = (request.form.get("doctor_phone") or "").strip()[:40]
        clinic = (request.form.get("doctor_clinic") or "").strip()[:200]
        bio = (request.form.get("doctor_bio") or "").strip()[:500]
        if len(full_name) < 2:
            flash("Please enter your display name (at least 2 characters).")
            return render_template(
                "doctor_profile.html",
                theme=theme,
                full_name=full_name,
                doctor_specialty=specialty,
                doctor_phone=phone,
                doctor_clinic=clinic,
                doctor_bio=bio,
            )
        db.execute(
            """
            UPDATE users
            SET full_name = ?, doctor_specialty = ?, doctor_phone = ?, doctor_clinic = ?, doctor_bio = ?
            WHERE id = ?
            """,
            (
                encrypt_text(full_name),
                specialty,
                encrypt_text(phone),
                encrypt_text(clinic),
                encrypt_text(bio),
                current_user.id,
            ),
        )
        db.commit()
        flash("Your profile was updated.")
        return redirect(url_for("core.doctor_profile"))

    return render_template(
        "doctor_profile.html",
        theme=theme,
        full_name=decrypt_text(row["full_name"]),
        doctor_specialty=(row["doctor_specialty"] or "").strip(),
        doctor_phone=decrypt_text(row["doctor_phone"]),
        doctor_clinic=decrypt_text(row["doctor_clinic"]),
        doctor_bio=decrypt_text(row["doctor_bio"]),
    )


@core_bp.get("/doctor/emergency-scan")
@login_required
def doctor_emergency_scan():
    if _portal_role(current_user.id) != "doctor":
        flash("That tool is for doctor accounts only.")
        return redirect(url_for("core.dashboard"))
    theme = _portal_theme(current_user.id, current_user.username)
    return render_template("doctor_emergency_scan.html", theme=theme)


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
    patients = db.execute(
        "SELECT full_name FROM doctor_patients WHERE doctor_user_id = ? ORDER BY full_name ASC",
        (current_user.id,),
    ).fetchall()
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
        db.commit()
        msg = f"Prescription saved for {patient_name}: {medicine_name}"
        if duration_days:
            msg += f" ({duration_days} day course)."
        if sent_to_patient:
            msg += " Marked as sent to patient (visible in your records; patient app delivery is coming soon)."
        flash(msg + ".")
        return redirect(url_for("core.doctor_dashboard"))
    return render_template("doctor_prescription_form.html", theme=theme, patients=patients)


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


@core_bp.get("/doctor/appointments")
@login_required
def doctor_appointments_all():
    redir = _require_doctor()
    if redir:
        return redir
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    rows = db.execute(
        """
        SELECT patient_name, reason, visit_ts, status
        FROM doctor_appointments
        WHERE doctor_user_id = ?
        ORDER BY visit_ts DESC
        LIMIT 200
        """,
        (current_user.id,),
    ).fetchall()
    items = []
    for row in rows:
        st = row["status"] or "scheduled"
        items.append(
            {
                "name": row["patient_name"],
                "sub": (row["reason"] or "").strip() or "Visit",
                "when": datetime.fromtimestamp(int(row["visit_ts"])).strftime("%Y-%m-%d %I:%M %p"),
                "status": st,
            }
        )
    return render_template("doctor_appointments_list.html", theme=theme, items=items)


@core_bp.get("/analytics")
@login_required
def patient_analytics():
    if _portal_role(current_user.id) == "doctor":
        return redirect(url_for("core.doctor_analytics"))
    
    db = get_db()
    theme = _portal_theme(current_user.id, current_user.username)
    uid = current_user.id
    
    # Patient-specific analytics data
    now = datetime.now()
    months: list[str] = []
    y, mo = now.year, now.month
    for _ in range(6):
        months.insert(0, f"{y:04d}-{mo:02d}")
        mo -= 1
        if mo == 0:
            mo = 12
            y -= 1
    
    trend_labels = [datetime(int(m[:4]), int(m[5:7]), 1).strftime("%b %y") for m in months]
    
    # File upload trends
    trend_values: list[int] = []
    for m in months:
        n = db.execute(
            """
            SELECT COUNT(*) AS c FROM files
            WHERE user_id = ?
              AND strftime('%Y-%m', datetime(uploaded_at, 'unixepoch')) = ?
            """,
            (uid, m),
        ).fetchone()["c"]
        trend_values.append(int(n))
    
    # Health data categories (from uploaded files)
    category_counts = db.execute(
        """
        SELECT category, COUNT(*) as count FROM files
        WHERE user_id = ? AND category != 'Uncategorized'
        GROUP BY category
        ORDER BY count DESC
        """,
        (uid,),
    ).fetchall()
    
    if category_counts:
        disease_labels = [row["category"] for row in category_counts]
        disease_values = [row["count"] for row in category_counts]
    else:
        # Demo data if no real data
        disease_labels = ["Lab Reports", "Prescriptions", "Scans", "Blood Reports"]
        disease_values = [8, 6, 4, 3]
    
    # Health engagement index (based on various activities)
    recovery_labels = trend_labels[:]
    recovery_values: list[float] = []
    
    for i, m in enumerate(months):
        # Count various health activities
        file_count = db.execute(
            "SELECT COUNT(*) AS c FROM files WHERE user_id = ? AND strftime('%Y-%m', datetime(uploaded_at, 'unixepoch')) = ?",
            (uid, m),
        ).fetchone()["c"]
        
        appointment_count = db.execute(
            "SELECT COUNT(*) AS c FROM appointments WHERE user_id = ? AND strftime('%Y-%m', datetime(created_at, 'unixepoch')) = ?",
            (uid, m),
        ).fetchone()["c"]
        
        prescription_count = db.execute(
            "SELECT COUNT(*) AS c FROM prescriptions WHERE user_id = ? AND strftime('%Y-%m', datetime(created_at, 'unixepoch')) = ?",
            (uid, m),
        ).fetchone()["c"]
        
        # Calculate engagement score
        activity_score = (file_count * 10) + (appointment_count * 15) + (prescription_count * 12)
        base_score = 65 + i * 2  # Improving trend
        final_score = min(95, max(70, base_score + activity_score))
        recovery_values.append(final_score)
    
    # Total counts
    total_files = db.execute("SELECT COUNT(*) AS c FROM files WHERE user_id = ?", (uid,)).fetchone()["c"]
    total_appointments = db.execute("SELECT COUNT(*) AS c FROM appointments WHERE user_id = ?", (uid,)).fetchone()["c"]
    total_prescriptions = db.execute("SELECT COUNT(*) AS c FROM prescriptions WHERE user_id = ?", (uid,)).fetchone()["c"]
    
    return render_template(
        "patient_analytics.html",
        title="Health Analytics — MedVault",
        theme=theme,
        chart={
            "trend_labels": trend_labels,
            "trend_values": trend_values,
            "disease_labels": disease_labels,
            "disease_values": disease_values,
            "recovery_labels": recovery_labels,
            "recovery_values": recovery_values,
            "total_files": total_files,
            "total_appointments": total_appointments,
            "total_prescriptions": total_prescriptions,
        },
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
            flash("Please fill in all required fields.")
        else:
            # Combine date and time
            appointment_datetime = f"{appointment_date} {appointment_time}"
            
            # Insert appointment
            db.execute(
                """
                INSERT INTO appointments (user_id, doctor_id, appointment_at, reason, status, created_at)
                VALUES (?, ?, ?, ?, 'scheduled', ?)
                """,
                (current_user.id, int(doctor_id), appointment_datetime, reason, now_ts()),
            )
            db.commit()
            
            flash("Appointment booked successfully!")
            return redirect(url_for("core.dashboard"))
    
    return render_template(
        "book_appointment.html",
        title="Book Appointment — MedVault",
        theme=theme,
        doctors=doctors
    )


@core_bp.get("/onboarding")
@login_required
def onboarding():
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
    db.execute("UPDATE users SET onboarding_done = 1 WHERE id = ?", (current_user.id,))
    db.commit()
    flash("Onboarding complete. Welcome!")
    if _portal_role(current_user.id) == "doctor":
        return redirect(url_for("core.doctor_dashboard"))
    return redirect(url_for("core.dashboard"))


@core_bp.post("/profile")
@login_required
def update_profile():
    db = get_db()
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


@core_bp.post("/upload")
@login_required
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Please choose a file.")
        return _redirect_home()
    if not allowed_file(f.filename):
        flash("Allowed file types: PDF, JPG, PNG.")
        return _redirect_home()

    original = secure_filename(f.filename)
    stored_name = f"{current_user.id}_{now_ts()}_{secrets.token_hex(8)}_{original}"
    
    # Use writeable /tmp/uploads folder to store file temporarily for OCR
    temp_dir = os.path.join("/tmp", "uploads")
    os.makedirs(temp_dir, exist_ok=True)
    saved_path = os.path.join(temp_dir, stored_name)
    f.save(saved_path)
    
    # Extract text from image files for AI categorization
    extracted_text = ""
    ext = original.rsplit(".", 1)[1].lower()
    if ext in {"png", "jpg", "jpeg"}:
        extracted_text = extract_text_with_tesseract(saved_path)
    
    # Use enhanced AI categorization with extracted text
    category, confidence, metadata = ai_categorize_medical_file(original, extracted_text)
    
    doc_category = (request.form.get("doc_category") or "").strip()
    doc_source = (request.form.get("doc_source") or "").strip()

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
            return _redirect_home()
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
        INSERT INTO files (user_id, filename, stored_path, category, category_confidence, doc_category, doc_source, extracted_text, uploaded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            now_ts(),
        ),
    )
    db.commit()
    
    # Enhanced feedback message with AI insights
    confidence_pct = int(confidence * 100)
    if confidence > 0.8:
        flash(f"🤖 AI categorized as: {category} ({confidence_pct}% confidence). {len(metadata.get('content_matches', []))} content patterns detected.")
    elif confidence > 0.5:
        flash(f"📋 AI categorized as: {category} ({confidence_pct}% confidence). Based on filename and content analysis.")
    else:
        flash(f"🔍 AI categorized as: {category} ({confidence_pct}% confidence). Consider updating category manually.")
    
    return _redirect_home()


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
        "SELECT stored_path FROM files WHERE id = ? AND user_id = ?",
        (file_id, current_user.id),
    ).fetchone()
    if not row:
        abort(404)
        
    stored_path = row["stored_path"]
    
    db.execute("DELETE FROM files WHERE id = ? AND user_id = ?", (file_id, current_user.id))
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


