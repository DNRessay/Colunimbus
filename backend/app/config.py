import os
import re
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass


def _env(key, default=""):
    return os.environ.get(key, default).strip()


def _flag(key, default="false"):
    return _env(key, default).lower() in ("1", "true", "yes", "on")


def _list(key, default=""):
    return [v.strip() for v in _env(key, default).split(",") if v.strip()]


class Settings:
    def __init__(self):
        self.secret_key = _env("SECRET_KEY", "dev-insecure-change-me")
        self.debug = _flag("DEBUG")
        self.database_url = _env("DATABASE_URL", "sqlite:///./lsuite.db")
        self.auto_create_tables = _flag("AUTO_CREATE_TABLES", "true")

        self.frontend_url = _env("FRONTEND_URL", "http://localhost:8788").rstrip("/")
        self.cors_origins = _list("CORS_ALLOWED_ORIGINS") or [self.frontend_url]
        # Cloudflare Pages also serves every deploy on <hash>.<project>.pages.dev; allow those previews.
        host = self.frontend_url.split("://", 1)[-1]
        default_regex = rf"https://([a-z0-9-]+\.)?{re.escape(host)}" if host.endswith(".pages.dev") else ""
        self.cors_origin_regex = _env("CORS_ORIGIN_REGEX", default_regex) or None
        # Public base URL of this API (Lambda Function URL). Used to build OAuth redirect URIs.
        self.api_url = _env("API_URL").rstrip("/")

        self.access_token_minutes = int(_env("ACCESS_TOKEN_MINUTES", "30"))
        self.refresh_token_days = int(_env("REFRESH_TOKEN_DAYS", "7"))

        self.google_client_id = _env("GOOGLE_CLIENT_ID")
        self.google_client_secret = _env("GOOGLE_CLIENT_SECRET")
        self.google_redirect_uri = _env("GOOGLE_REDIRECT_URI")
        self.github_client_id = _env("GITHUB_CLIENT_ID")
        self.github_client_secret = _env("GITHUB_CLIENT_SECRET")
        self.facebook_app_id = _env("FACEBOOK_APP_ID")
        self.facebook_app_secret = _env("FACEBOOK_APP_SECRET")

        self.email_host = _env("EMAIL_HOST")
        self.email_port = int(_env("EMAIL_PORT", "587"))
        self.email_user = _env("EMAIL_HOST_USER")
        self.email_password = _env("EMAIL_HOST_PASSWORD")
        self.email_use_tls = _flag("EMAIL_USE_TLS", "true")
        self.default_from_email = _env("DEFAULT_FROM_EMAIL", "LSuite <noreply@example.com>")

        self.groq_api_keys = _list("GROQ_API_KEYS") or _list("GROQ_API_KEY")
        self.groq_model = _env("GROQ_MODEL", "llama-3.3-70b-versatile")

        # Set by the SAM template. Empty locally -> jobs run in a background thread.
        self.worker_function = _env("WORKER_FUNCTION_NAME")
        self.uploads_bucket = _env("UPLOADS_BUCKET")

        self.mysql_ssl_ca = _env("MYSQL_SSL_CA")


settings = Settings()
