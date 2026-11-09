import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-change-me")
    APP_NAME = os.getenv("APP_NAME", "Eda")
    APP_URL = os.getenv("APP_URL", "")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'instance' / 'eda.db'}")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    FRESH_CORE_SHARED_SECRET = os.getenv("FRESH_CORE_SHARED_SECRET", "")
    FRESH_CRACKERS_SSO_URL = os.getenv("FRESH_CRACKERS_SSO_URL", "")
    FLEXIBEE_BASE_URL = os.getenv("FLEXIBEE_BASE_URL", "")
    FLEXIBEE_COMPANY = os.getenv("FLEXIBEE_COMPANY", "")
    FLEXIBEE_USERNAME = os.getenv("FLEXIBEE_USERNAME", "")
    FLEXIBEE_PASSWORD = os.getenv("FLEXIBEE_PASSWORD", "")
    FLEXIBEE_VERIFY_SSL = os.getenv("FLEXIBEE_VERIFY_SSL", "1") == "1"
    FLEXIBEE_INVOICE_TYPE_CODE = os.getenv("FLEXIBEE_INVOICE_TYPE_CODE", "FAKTURA")
    FLEXIBEE_DEFAULT_DUE_DAYS = int(os.getenv("FLEXIBEE_DEFAULT_DUE_DAYS", "14"))
    DIAGNOSTIC_TOKEN = os.getenv("DIAGNOSTIC_TOKEN", "")
    AI_ENGINE_URL = os.getenv("AI_ENGINE_URL", "https://aiengine.freshcrackers.cz")
    AI_ENGINE_API_KEY = os.getenv("AI_ENGINE_API_KEY") or os.getenv("FRESH_AI_ENGINE_API_KEY", "")
    AI_ENGINE_APP_ID = os.getenv("AI_ENGINE_APP_ID", "eda")
    AI_ENGINE_QUALITY = os.getenv("AI_ENGINE_QUALITY", "balanced")
    AI_ENGINE_MAX_COST_USD = float(os.getenv("AI_ENGINE_MAX_COST_USD", "0.20"))
    AI_ENGINE_TIMEOUT_SECONDS = int(os.getenv("AI_ENGINE_TIMEOUT_SECONDS", "90"))
    AI_ENGINE_ATTACHMENT_MAX_MB = int(os.getenv("AI_ENGINE_ATTACHMENT_MAX_MB", "8"))
    AI_ENGINE_ATTACHMENT_TEXT_MAX_CHARS = int(os.getenv("AI_ENGINE_ATTACHMENT_TEXT_MAX_CHARS", "60000"))
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH_MB", "20")) * 1024 * 1024
