"""Structured logging.

The server used bare ``print(..., flush=True)``, which journalctl captures as
unstructured lines: you cannot filter by device, by outcome, or by request, and
a request's lines are scattered with no way to tie them together.

Two formats, one switch:

* ``text`` (default) — human readable, ``key=value`` pairs, greppable.
* ``json`` — one object per line, for journald/Loki/ELK.

Every line carries a request id when one is in scope, so the log for a single
request can be extracted with one filter.

Secrets are redacted at the formatter, not at each call site. A field whose
name looks like a credential is masked, and so is any *value* that exactly
matches a configured secret — which covers the case where a secret is passed
under an innocent-looking name.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import threading
import time
from datetime import datetime, timezone

REQUEST_ID_HEADER = "X-Request-ID"

# Field names whose values must never reach the log.
_SENSITIVE_NAME = re.compile(
    r"(pass|secret|token|api[_-]?key|authorization|cookie|credential)",
    re.I,
)

# Suffixes marking a value as derived metadata rather than the thing itself:
# a sha256 fingerprint, a length, a count, a timestamp, an id. `api_key_fp` is
# the safe form of `api_key` and must survive the name rule, while
# value-level redaction below still applies to it.
_METADATA_SUFFIX = re.compile(
    r"_(fp|len|count|at|ms|id|fingerprint|digest|prefix)$",
    re.I,
)

REDACTED = "[redacted]"

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "c2_request_id", default=None
)
_fields: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "c2_log_fields", default=None
)

# Secrets registered for value-level redaction. Populated at startup.
_secret_values: set[str] = set()
_lock = threading.Lock()


# ------------------------------------------------------------------ config

def configure(level: str = "info", fmt: str = "text",
              secrets: list[str] | None = None) -> logging.Logger:
    """Install our handler on the ``c2`` logger and return it.

    The handler goes on ``c2``, not on root: this keeps the format from being
    forced onto third-party libraries while still letting our records
    propagate normally.
    """
    logger = logging.getLogger("c2")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.propagate = False

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(TextFormatter() if fmt != "json" else JsonFormatter())

    logger.addHandler(handler)
    logger.setLevel(_LEVELS.get(str(level).lower(), logging.INFO))

    # Werkzeug logs a line per request, including a spurious 500 for every
    # WebSocket that closes. We emit our own request log, so silence it.
    werkzeug = logging.getLogger("werkzeug")
    werkzeug.setLevel(logging.WARNING)
    werkzeug.propagate = False
    if not werkzeug.handlers:
        werkzeug.addHandler(logging.NullHandler())

    register_secrets(secrets or [])
    return logger


def register_secrets(values) -> None:
    """Register exact values to be masked wherever they appear."""
    with _lock:
        for value in values:
            if value and isinstance(value, str) and len(value) >= 6:
                _secret_values.add(value)


def secret_snapshot() -> set[str]:
    with _lock:
        return set(_secret_values)


# ----------------------------------------------------------------- context

def set_request_id(value: str | None) -> None:
    _request_id.set(value or None)


def get_request_id() -> str | None:
    return _request_id.get()


def bind(**fields) -> None:
    """Add fields to every subsequent line in this context."""
    current = dict(_fields.get() or {})
    current.update({k: v for k, v in fields.items() if v is not None})
    _fields.set(current)


def unbind_all() -> None:
    _fields.set(None)


def new_request_id() -> str:
    import secrets as _secrets

    return _secrets.token_hex(8)


# --------------------------------------------------------------- redaction

def redact_value(name: str, value):
    if name and _SENSITIVE_NAME.search(name) and not _METADATA_SUFFIX.search(name):
        return REDACTED
    if isinstance(value, str) and len(value) >= 6:
        with _lock:
            for secret in _secret_values:
                if secret in value:
                    return value.replace(secret, REDACTED)
    return value


# -------------------------------------------------------------- formatting

class _Base(logging.Formatter):
    def _base(self, record: logging.LogRecord) -> dict:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
                    .astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }

        rid = _request_id.get()
        if rid:
            payload["rid"] = rid

        bound = _fields.get() or {}
        for key, value in bound.items():
            payload.setdefault(key, value)

        extra = getattr(record, "structured", None)
        if extra:
            for key, value in extra.items():
                if value is not None:
                    payload[key] = value

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return {k: redact_value(k, v) for k, v in payload.items()}

    def format(self, record: logging.LogRecord) -> str:
        raise NotImplementedError


class TextFormatter(_Base):
    """`12:34:56.789 INFO c2.agent ws.accept device=abc123 rid=deadbeef`"""

    def format(self, record: logging.LogRecord) -> str:
        data = self._base(record)
        ts = data.pop("ts")[11:23]
        level = data.pop("level").upper()[:4]
        name = data.pop("logger")
        event = data.pop("event")

        parts = [f"{ts} {level:<4} {name} {event}"]
        for key, value in data.items():
            if key in ("rid", "exc"):
                continue
            parts.append(f"{key}={_scalar(value)}")

        if "rid" in data:
            parts.append(f"rid={data['rid']}")
        if "exc" in data:
            parts.append(f"\n{data['exc']}")

        return " ".join(parts)


class JsonFormatter(_Base):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(self._base(record), default=str, sort_keys=False)


def _scalar(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if not text or any(c.isspace() for c in text):
        return json.dumps(text)
    return text


# ------------------------------------------------------------------ helper

def log(logger: logging.Logger, level: str, event: str, **fields) -> None:
    """Emit a structured line. Use this instead of print()."""
    logger.log(_LEVELS.get(level, logging.INFO), event, extra={"structured": fields})


def get_logger(name: str = "c2") -> logging.Logger:
    return logging.getLogger(name)


def now_ms() -> int:
    return int(time.time() * 1000)