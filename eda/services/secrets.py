import base64
import hashlib
from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


def _fernet():
    secret = str(current_app.config.get("SECRET_KEY") or "")
    if not secret:
        raise RuntimeError("SECRET_KEY není nastaven; citlivé údaje nelze bezpečně uložit.")
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_text(value):
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    return _fernet().encrypt(text.encode("utf-8")).decode("ascii")


def decrypt_text(value):
    if not value:
        return ""
    try:
        return _fernet().decrypt(str(value).encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        raise RuntimeError("Uložený tajný údaj nelze dešifrovat. Zkontroluj, že se nezměnil SECRET_KEY.") from exc


def mask_secret(value):
    if not value:
        return ""
    text = str(value)
    if len(text) <= 8:
        return "••••••••"
    return text[:4] + "••••••••" + text[-4:]
