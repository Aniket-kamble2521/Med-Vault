from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_user, logout_user

from app_pkg.db import get_db
from app_pkg.extensions import login_manager
from app_pkg.models import User
from app_pkg.services.security import hash_password, verify_password

auth_bp = Blueprint("auth", __name__)


@login_manager.user_loader
def load_user(user_id: str):
    db = get_db()
    row = db.execute("SELECT id, username, portal_role FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return None
    return User(row["id"], row["username"], row["portal_role"])


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    role = _login_role()
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        effective_role = (_login_role() or "patient").lower()
        if effective_role not in ("doctor", "patient"):
            effective_role = "patient"
        if len(username) < 3 or len(password) < 6:
            flash("Username must be >= 3 chars and password >= 6 chars.")
            return render_template("register.html", role=effective_role)

        db = get_db()
        try:
            cur = db.execute(
                "INSERT INTO users (username, password_hash, portal_role) VALUES (?, ?, ?)",
                (username, hash_password(password), effective_role),
            )
            uid = cur.lastrowid
            if effective_role == "patient" and uid:
                db.execute(
                    "UPDATE users SET theme_accent = ?, theme_mode = ? WHERE id = ?",
                    ("#334155", "light", uid),
                )
            db.commit()
            flash("Account created. Please log in.")
            return redirect(url_for("auth.login", role=effective_role))
        except Exception:
            flash("That username is already taken for this role.")
            return render_template("register.html", role=effective_role)
    return render_template("register.html", role=role)


def _login_role() -> str | None:
    raw = (request.values.get("role") or "").strip().lower()
    if raw in ("doctor", "patient"):
        return raw
    return None


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        role = (_login_role() or "patient").lower()
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        db = get_db()
        row = db.execute("SELECT id, username, password_hash, portal_role FROM users WHERE username = ? AND portal_role = ?", (username, role)).fetchone()
        if not row:
            other_role = "patient" if role == "doctor" else "doctor"
            other_row = db.execute("SELECT id FROM users WHERE username = ? AND portal_role = ?", (username, other_role)).fetchone()
            if other_row:
                if other_role == "patient":
                    flash("This account is registered as a Patient. Please use the Patient Login page.")
                else:
                    flash("This account is registered as a Doctor. Please use the Doctor Login page.")
            else:
                flash("Invalid username or password.")
            return render_template("login.html", role=role)

        ok, needs_migration = verify_password(password, row["password_hash"])
        if not ok:
            flash("Invalid username or password.")
            return render_template("login.html", role=role)
        if needs_migration:
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), row["id"]))
            db.commit()

        login_user(User(row["id"], row["username"], row["portal_role"]))
        
        db_role = (row["portal_role"] or "patient").lower()
        if db_role == "doctor":
            return redirect(url_for("core.doctor_dashboard"))
        return redirect(url_for("core.dashboard"))
    return render_template("login.html", role=_login_role())


@auth_bp.route("/logout", methods=["GET", "POST"])
def logout():
    logout_user()
    return redirect(url_for("core.index"))

