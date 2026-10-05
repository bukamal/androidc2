"""Runtime configuration.

Secrets are never hardcoded. Resolution order for every secret:

    1. environment variable
    2. DATA_DIR/<NAME>.secret   (generated once on first run, mode 0600)
    3. random value, written to (2)

Nothing here has a usable default, so a checkout can never ship with a
credential that is already public in git history.
"""

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Everything mutable lives here: uploads, generated secrets.
DATA_DIR = Path(os.environ.get("C2_DATA_DIR") or (BASE_DIR / "data"))


def _load_secret(env_name: str) -> str:
    value = os.environ.get(env_name)
    if value:
        return value.strip()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{env_name.lower()}.secret"

    if path.exists():
        stored = path.read_text(encoding="utf-8").strip()
        if stored:
            return stored

    value = secrets.token_urlsafe(32)
    path.write_text(value + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value


def _operator_password() -> str:
    """Operator password.

    Env var wins. Otherwise a random one is generated and written to
    DATA_DIR so it survives restarts and can be read back by the operator.
    """
    value = os.environ.get("C2_OPERATOR_PASSWORD")
    if value:
        return value
    return _load_secret("C2_OPERATOR_PASSWORD")


class Config:
    SECRET_KEY = _load_secret("C2_SECRET")

    # Shared secret between operator panel and agents. Must be set to the same
    # value that the agent APK was built with.
    API_KEY = _load_secret("C2_API_KEY")

    OPERATOR_PASSWORD = _operator_password()
    # Display name recorded in the audit trail. Distinguishes operators only
    # if you change it per person; a shared panel can only attribute to
    # "operator" plus a session id.
    OPERATOR_NAME = os.environ.get("C2_OPERATOR_NAME", "operator")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 12  # 12h

    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "C2_DB_URI",
        f"sqlite:///{BASE_DIR / 'c2.db'}",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Captured data lives outside static/ so it is never served by the
    # static file handler. Access goes through authenticated routes only.
    UPLOAD_FOLDER = os.environ.get(
        "C2_UPLOAD_FOLDER",
        str(DATA_DIR / "uploads"),
    )
    # Where captures used to live before the move; drained on first startup.
    LEGACY_UPLOAD_FOLDER = os.environ.get(
        "C2_LEGACY_UPLOAD_FOLDER",
        str(BASE_DIR / "static" / "uploads"),
    )

    MAX_CONTENT_LENGTH = int(
        os.environ.get("C2_MAX_UPLOAD_MB", "1024")
    ) * 1024 * 1024

    # ── Access ──────────────────────────────────────────────────────────────
    # "open"    — everything open. Safe only while bound to loopback.
    #             This is the default, so `python3 app.py` just works.
    # "token"   — loopback stays open; anything arriving from a non-loopback
    #             address must present C2_ACCESS_TOKEN. Use this before
    #             putting the panel behind a tunnel.
    # "operator"— named operator accounts (see c2/operators.py).
    AUTH_MODE = os.environ.get("C2_AUTH", "open").lower()

    # Shared secret for AUTH_MODE="token". Reuses the same idea as
    # C2_API_KEY: one value, set once, no login form.
    ACCESS_TOKEN = os.environ.get("C2_ACCESS_TOKEN", "")

    # Logging: "text" for a human, "json" for journald/Loki/ELK.
    LOG_LEVEL = os.environ.get("C2_LOG_LEVEL", "info")
    LOG_FORMAT = os.environ.get("C2_LOG_FORMAT", "text")
    # Trust X-Request-ID from a reverse proxy you control (Caddy does not set
    # it). Off by default so a client cannot spoof correlation ids.
    TRUST_PROXY_REQUEST_ID = os.environ.get(
        "C2_TRUST_PROXY_REQUEST_ID", ""
    ).lower() in ("1", "true", "yes")

    AGENT_HEARTBEAT_TIMEOUT = int(os.environ.get("C2_HEARTBEAT_TIMEOUT", "60"))
    AGENT_REAPER_INTERVAL = int(os.environ.get("C2_REAPER_INTERVAL", "15"))

    # Loopback by default: the only way in should be an explicit tunnel you
    # control, not every interface on the box.
    C2_HOST = os.environ.get("C2_HOST", "127.0.0.1")
    C2_PORT = int(os.environ.get("C2_PORT", "5000"))