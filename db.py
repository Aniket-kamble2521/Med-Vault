import os
import sqlite3
from flask import current_app, g


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db_path = current_app.config["DATABASE"]
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


def close_db(e=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = get_db()
    schema_path = current_app.config["SCHEMA_PATH"]
    with open(schema_path, "r", encoding="utf-8") as f:
        db.executescript(f.read())
    _run_safe_migrations(db)
    db.commit()


def _run_safe_migrations(db: sqlite3.Connection) -> None:
    cols = {
        row["name"]
        for row in db.execute("PRAGMA table_info(files)").fetchall()
    }
    if "category" not in cols:
        db.execute(
            "ALTER TABLE files ADD COLUMN category TEXT NOT NULL DEFAULT 'Uncategorized'"
        )
    if "category_confidence" not in cols:
        db.execute(
            "ALTER TABLE files ADD COLUMN category_confidence REAL NOT NULL DEFAULT 0.0"
        )
