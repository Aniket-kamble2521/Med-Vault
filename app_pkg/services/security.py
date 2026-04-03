import base64
import os
import secrets

import bcrypt
from cryptography.fernet import Fernet, InvalidToken
from flask import current_app
from werkzeug.security import check_password_hash


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, stored_hash: str) -> tuple[bool, bool]:
    if stored_hash.startswith("$2"):
        return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8")), False
    ok = check_password_hash(stored_hash, password)
    return ok, ok


def generate_secret_token() -> str:
    return secrets.token_urlsafe(32)


def _get_cipher() -> Fernet:
    key_from_env = os.environ.get("FERNET_KEY")
    if key_from_env:
        return Fernet(key_from_env.encode("utf-8"))
    key_path = os.path.join(current_app.instance_path, "fernet.key")
    if os.path.exists(key_path):
        with open(key_path, "rb") as f:
            key = f.read().strip()
    else:
        key = Fernet.generate_key()
        with open(key_path, "wb") as f:
            f.write(key)
    return Fernet(key)


def encrypt_text(value: str) -> str:
    if not value:
        return ""
    token = _get_cipher().encrypt(value.encode("utf-8"))
    return base64.urlsafe_b64encode(token).decode("utf-8")


def decrypt_text(value: str) -> str:
    if not value:
        return ""
    try:
        token = base64.urlsafe_b64decode(value.encode("utf-8"))
        return _get_cipher().decrypt(token).decode("utf-8")
    except (InvalidToken, ValueError):
        return value

