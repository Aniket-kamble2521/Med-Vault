import time

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from app_pkg.db import get_db

modules_bp = Blueprint("modules", __name__)


def now_ts() -> int:
    return int(time.time())


# All forms now redirect to their dedicated subpages instead of the deprecated roadmap module.

@modules_bp.post("/family")
@login_required
def add_family_profile():
    from flask_login import current_user

    db = get_db()
    db.execute(
        """
        INSERT INTO family_profiles (owner_user_id, profile_name, relationship, age, emergency_contact, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            current_user.id,
            (request.form.get("profile_name") or "").strip(),
            (request.form.get("relationship") or "self").strip(),
            request.form.get("age") or None,
            (request.form.get("emergency_contact") or "").strip(),
            now_ts(),
        ),
    )
    db.commit()
    flash("Family profile added.")
    return redirect(url_for("core.dashboard"))


@modules_bp.post("/timeline")
@login_required
def add_timeline():
    from flask_login import current_user

    db = get_db()
    db.execute(
        """
        INSERT INTO medical_timeline_events (user_id, event_date, event_type, description, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            current_user.id,
            (request.form.get("event_date") or "").strip(),
            (request.form.get("event_type") or "general").strip(),
            (request.form.get("description") or "").strip(),
            now_ts(),
        ),
    )
    db.commit()
    flash("Timeline event added.")
    return redirect(url_for("core.health_timeline"))


@modules_bp.post("/prescriptions")
@login_required
def add_prescription():
    from flask_login import current_user

    db = get_db()
    db.execute(
        """
        INSERT INTO prescriptions (user_id, medicine_name, dosage, frequency, doctor_name, start_date, end_date, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            current_user.id,
            (request.form.get("medicine_name") or "").strip(),
            (request.form.get("dosage") or "").strip(),
            (request.form.get("frequency") or "").strip(),
            (request.form.get("doctor_name") or "").strip(),
            (request.form.get("start_date") or "").strip(),
            (request.form.get("end_date") or "").strip(),
            now_ts(),
        ),
    )
    db.commit()
    flash("Prescription saved.")
    return redirect(url_for("core.dashboard"))


@modules_bp.post("/doctors")
@login_required
def add_doctor():
    from flask_login import current_user

    db = get_db()
    db.execute(
        "INSERT INTO doctors (user_id, name, specialization, contact, created_at) VALUES (?, ?, ?, ?, ?)",
        (
            current_user.id,
            (request.form.get("name") or "").strip(),
            (request.form.get("specialization") or "").strip(),
            (request.form.get("contact") or "").strip(),
            now_ts(),
        ),
    )
    db.commit()
    flash("Doctor added.")
    return redirect(url_for("core.favorite_doctors"))


@modules_bp.post("/appointments")
@login_required
def add_appointment():
    from flask_login import current_user

    db = get_db()
    doctor_id = request.form.get("doctor_id")
    db.execute(
        """
        INSERT INTO appointments (user_id, doctor_id, appointment_at, reason, visit_notes, referral_note, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            current_user.id,
            int(doctor_id) if doctor_id else None,
            (request.form.get("appointment_at") or "").strip(),
            (request.form.get("reason") or "").strip(),
            (request.form.get("visit_notes") or "").strip(),
            (request.form.get("referral_note") or "").strip(),
            (request.form.get("status") or "scheduled").strip(),
            now_ts(),
        ),
    )
    db.commit()
    flash("Appointment saved.")
    return redirect(url_for("core.health_timeline"))


@modules_bp.post("/vaccinations")
@login_required
def add_vaccination():
    from flask_login import current_user

    db = get_db()
    db.execute(
        """
        INSERT INTO vaccinations (user_id, vaccine_name, dose_info, due_date, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            current_user.id,
            (request.form.get("vaccine_name") or "").strip(),
            (request.form.get("dose_info") or "").strip(),
            (request.form.get("due_date") or "").strip(),
            (request.form.get("status") or "pending").strip(),
            now_ts(),
        ),
    )
    db.commit()
    flash("Vaccination record added.")
    return redirect(url_for("core.health_timeline"))


@modules_bp.post("/allergies")
@login_required
def add_allergy():
    from flask_login import current_user

    db = get_db()
    db.execute(
        """
        INSERT INTO allergies_registry (user_id, allergy_name, severity, notes, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            current_user.id,
            (request.form.get("allergy_name") or "").strip(),
            (request.form.get("severity") or "mild").strip(),
            (request.form.get("notes") or "").strip(),
            now_ts(),
        ),
    )
    db.commit()
    flash("Allergy added.")
    return redirect(url_for("core.dashboard"))


@modules_bp.post("/reminders")
@login_required
def add_reminder():
    from flask_login import current_user

    db = get_db()
    title = (request.form.get("title") or "").strip()
    remind_at = (request.form.get("remind_at") or "").strip()
    reminder_type = (request.form.get("reminder_type") or "general").strip()
    
    dosage = (request.form.get("dosage") or "").strip()
    instructions = (request.form.get("instructions") or "").strip()
    med_image = (request.form.get("med_image") or "").strip()
    repeat_enabled = 1 if request.form.get("repeat_enabled") else 0

    db.execute(
        """
        INSERT INTO reminders (user_id, title, remind_at, reminder_type, is_done, dosage, instructions, med_image, repeat_enabled, created_at)
        VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
        """,
        (
            current_user.id,
            title,
            remind_at,
            reminder_type,
            dosage,
            instructions,
            med_image,
            repeat_enabled,
            now_ts(),
        ),
    )
    db.commit()
    flash("Reminder added.")
    return redirect(url_for("core.medicine_reminders"))


@modules_bp.post("/reminders/<int:reminder_id>/done")
@login_required
def mark_reminder_done(reminder_id: int):
    from flask_login import current_user

    db = get_db()
    db.execute("UPDATE reminders SET is_done = 1 WHERE id = ? AND user_id = ?", (reminder_id, current_user.id))
    db.commit()
    flash("Reminder marked done.")
    return redirect(url_for("core.medicine_reminders"))


@modules_bp.post("/vitals")
@login_required
def add_vitals():
    from flask_login import current_user

    db = get_db()
    db.execute(
        """
        INSERT INTO vitals_logs (user_id, logged_at, bp_systolic, bp_diastolic, sugar, heart_rate, weight, symptoms, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            current_user.id,
            (request.form.get("logged_at") or "").strip(),
            request.form.get("bp_systolic") or None,
            request.form.get("bp_diastolic") or None,
            request.form.get("sugar") or None,
            request.form.get("heart_rate") or None,
            request.form.get("weight") or None,
            (request.form.get("symptoms") or "").strip(),
            now_ts(),
        ),
    )
    db.commit()
    flash("Vitals logged.")
    return redirect(url_for("core.health_vitals"))



def now_ts() -> int:
    return int(time.time())

