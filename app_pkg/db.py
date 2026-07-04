import os
import re
import sqlite3
from flask import current_app, g

# Detect if we should use PostgreSQL (via Supabase) or fallback to SQLite
DATABASE_URL = os.environ.get("DATABASE_URL")


class PgRow:
    def __init__(self, description, row_tuple):
        self._keys = [desc[0] for desc in description]
        self._row = row_tuple
        self._dict = dict(zip(self._keys, self._row))

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._row[key]
        return self._dict[key]

    def keys(self):
        return self._keys

    def __repr__(self):
        return repr(self._dict)

    def get(self, key, default=None):
        return self._dict.get(key, default)


class PgCursor:
    def __init__(self, pg_cursor):
        self._cursor = pg_cursor
        self._lastrowid = None

    def execute(self, sql, params=None):
        sql_stripped = sql.strip().upper()
        # Intercept and translate PRAGMA table_info(table_name)
        if "PRAGMA TABLE_INFO" in sql_stripped:
            match = re.search(r"PRAGMA\s+TABLE_INFO\((.*?)\)", sql, re.IGNORECASE)
            if match:
                table_name = match.group(1).strip("'\" ")
                sql = f"SELECT column_name AS name FROM information_schema.columns WHERE table_name = '{table_name}'"
                params = None

        # Replace SQLite style '?' with PostgreSQL style '%s'
        if params is not None:
            sql = sql.replace('?', '%s')

        # Translate AUTOINCREMENT keyword if found in in-line DDL (if any)
        sql = sql.replace("AUTOINCREMENT", "")

        is_insert = sql_stripped.startswith("INSERT")
        if is_insert and "RETURNING" not in sql_stripped:
            # Append RETURNING id to get the inserted row ID for lastrowid compatibility
            sql_clean = sql.rstrip('; ')
            sql_returning = sql_clean + " RETURNING id"
            try:
                self._cursor.execute(sql_returning, params)
                row = self._cursor.fetchone()
                if row:
                    self._lastrowid = row[0]
                else:
                    self._lastrowid = None
            except Exception:
                # If RETURNING id fails, fallback to executing standard statement
                self._cursor.execute(sql, params)
                self._lastrowid = None
            return self

        self._cursor.execute(sql, params)
        self._lastrowid = None
        return self

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        return PgRow(self._cursor.description, row)

    def fetchall(self):
        rows = self._cursor.fetchall()
        if not rows:
            return []
        desc = self._cursor.description
        return [PgRow(desc, r) for r in rows]

    @property
    def lastrowid(self):
        return self._lastrowid

    def close(self):
        self._cursor.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class PgConnection:
    def __init__(self, pg_conn):
        self._conn = pg_conn

    def cursor(self):
        return PgCursor(self._conn.cursor())

    def execute(self, sql, params=None):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def executescript(self, sql_script):
        # 1. Translate AUTOINCREMENT
        sql_script = sql_script.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
        # 2. Execute script via cursor
        with self._conn.cursor() as cur:
            cur.execute(sql_script)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, val):
        pass


def get_db():
    if "db" not in g:
        if DATABASE_URL:
            import psycopg2
            pg_conn = psycopg2.connect(DATABASE_URL)
            g.db = PgConnection(pg_conn)
        else:
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


def _run_safe_migrations(db) -> None:
    user_cols = {row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()}
    if "onboarding_done" not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN onboarding_done INTEGER NOT NULL DEFAULT 0")

    cols = {row["name"] for row in db.execute("PRAGMA table_info(files)").fetchall()}
    if "category" not in cols:
        db.execute("ALTER TABLE files ADD COLUMN category TEXT NOT NULL DEFAULT 'Uncategorized'")
    if "category_confidence" not in cols:
        # SQLite uses REAL, PostgreSQL uses REAL / DOUBLE PRECISION
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


def _ensure_doctor_tables(db) -> None:
    # Executescript translates AUTOINCREMENT to SERIAL PRIMARY KEY for Postgres compatibility
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


def _migrate_doctor_prescriptions(db) -> None:
    cols = {row["name"] for row in db.execute("PRAGMA table_info(doctor_prescriptions)").fetchall()}
    if cols and "duration_days" not in cols:
        db.execute("ALTER TABLE doctor_prescriptions ADD COLUMN duration_days INTEGER NOT NULL DEFAULT 0")
    if cols and "sent_to_patient" not in cols:
        db.execute("ALTER TABLE doctor_prescriptions ADD COLUMN sent_to_patient INTEGER NOT NULL DEFAULT 0")
