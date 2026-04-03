import sqlite3

from flask import current_app, g


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


def close_db(e=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = get_db()
    with open(current_app.config["SCHEMA_PATH"], "r", encoding="utf-8") as f:
        db.executescript(f.read())
    _run_safe_migrations(db)
    db.commit()


def _run_safe_migrations(db: sqlite3.Connection) -> None:
    user_cols = {row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()}
    if "onboarding_done" not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN onboarding_done INTEGER NOT NULL DEFAULT 0")

    cols = {row["name"] for row in db.execute("PRAGMA table_info(files)").fetchall()}
    if "category" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN category TEXT NOT NULL DEFAULT 'Uncategorized'")
    if "category_confidence" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN category_confidence REAL NOT NULL DEFAULT 0.0")
    if "doc_category" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN doc_category TEXT NOT NULL DEFAULT ''")
    if "doc_source" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN doc_source TEXT NOT NULL DEFAULT ''")
    if "extracted_text" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN extracted_text TEXT NOT NULL DEFAULT ''")

    user_cols = {row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()}
    if "portal_role" not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN portal_role TEXT NOT NULL DEFAULT 'patient'")
    if "theme_accent" not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN theme_accent TEXT NOT NULL DEFAULT '#3b82f6'")
    if "theme_mode" not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN theme_mode TEXT NOT NULL DEFAULT 'light'")
    if "doctor_specialty" not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN doctor_specialty TEXT NOT NULL DEFAULT ''")
    if "doctor_phone" not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN doctor_phone TEXT NOT NULL DEFAULT ''")
    if "doctor_clinic" not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN doctor_clinic TEXT NOT NULL DEFAULT ''")
    if "doctor_bio" not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN doctor_bio TEXT NOT NULL DEFAULT ''")

    _ensure_doctor_tables(db)


def _ensure_doctor_tables(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS doctor_patients (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          doctor_user_id INTEGER NOT NULL,
          full_name TEXT NOT NULL,
          phone TEXT NOT NULL DEFAULT '',
          notes TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL,
          FOREIGN KEY (doctor_user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS doctor_appointments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          doctor_user_id INTEGER NOT NULL,
          patient_name TEXT NOT NULL,
          reason TEXT NOT NULL DEFAULT '',
          visit_ts INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'scheduled',
          created_at INTEGER NOT NULL,
          FOREIGN KEY (doctor_user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS doctor_prescriptions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          doctor_user_id INTEGER NOT NULL,
          patient_name TEXT NOT NULL,
          medicine_name TEXT NOT NULL,
          dosage TEXT NOT NULL DEFAULT '',
          frequency TEXT NOT NULL DEFAULT '',
          notes TEXT NOT NULL DEFAULT '',
          duration_days INTEGER NOT NULL DEFAULT 0,
          sent_to_patient INTEGER NOT NULL DEFAULT 0,
          created_at INTEGER NOT NULL,
          FOREIGN KEY (doctor_user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS doctor_consultations (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          doctor_user_id INTEGER NOT NULL,
          patient_name TEXT NOT NULL,
          chief_complaint TEXT NOT NULL DEFAULT '',
          notes TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'in_progress',
          created_at INTEGER NOT NULL,
          FOREIGN KEY (doctor_user_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_dr_appt_doctor_ts ON doctor_appointments(doctor_user_id, visit_ts);
        CREATE INDEX IF NOT EXISTS idx_dr_pt_doctor ON doctor_patients(doctor_user_id);
        CREATE TABLE IF NOT EXISTS doctor_smart_alerts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          doctor_user_id INTEGER NOT NULL,
          alert_type TEXT NOT NULL,
          title TEXT NOT NULL,
          body TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          read_at INTEGER,
          FOREIGN KEY (doctor_user_id) REFERENCES users(id)
        );
        """
    )
    _migrate_doctor_prescriptions(db)


def _migrate_doctor_prescriptions(db: sqlite3.Connection) -> None:
    cols = {row["name"] for row in db.execute("PRAGMA table_info(doctor_prescriptions)").fetchall()}
    if cols and "duration_days" not in cols:
        db.execute("ALTER TABLE doctor_prescriptions ADD COLUMN duration_days INTEGER NOT NULL DEFAULT 0")
    if cols and "sent_to_patient" not in cols:
        db.execute("ALTER TABLE doctor_prescriptions ADD COLUMN sent_to_patient INTEGER NOT NULL DEFAULT 0")

