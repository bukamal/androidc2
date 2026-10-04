"""Authentication for the operator panel and for agent callbacks.

Two separate trust domains:

* **Operator** — a human with a password. Cookie session, CSRF token, and a
  login throttle. Guards every dashboard route and every ``/api/*`` operator
  endpoint.
* **Agent** — a device holding the shared API key. Key comparison is
  constant-time. Agents are additionally *scoped to their own device id* so
  one device cannot read or complete another device's commands.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from functools import wraps

from flask import jsonify, redirect, request, session, url_for

from config import Config

# --------------------------------------------------------------------------
# Shared-key check (agents)
# --------------------------------------------------------------------------


def agent_key_ok(candidate: str | None) -> bool:
    """Constant-time comparison against the shared agent key."""
    if not candidate:
        return False
    expected = (Config.API_KEY or "").encode("utf-8")
    given = str(candidate).encode("utf-8")
    return hmac.compare_digest(given, expected)


def key_fingerprint(value: str | None) -> str:
    """Short, non-reversible tag so two processes can compare keys safely.

    Never log a key. When an agent refuses to connect the one question that
    matters is "is the key in the APK the same as the key the server
    expects", and two 8-character tags answer that without printing either
    secret. ``--------`` means no key arrived at all.
    """
    if not value:
        return "--------"
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:8]


# --------------------------------------------------------------------------
# Login throttling
# --------------------------------------------------------------------------

_LOCK = threading.Lock()
_FAILURES: dict[str, list[float]] = {}

WINDOW_SECONDS = 300.0
MAX_ATTEMPTS = 5
BLOCK_SECONDS = 60.0


def _client_ip() -> str:
    # Only trust X-Forwarded-For when you actually run behind a proxy you
    # control; otherwise remote_addr is the only honest value.
    return request.remote_addr or "unknown"


def _prune(ip: str, now: float) -> list[float]:
    return [t for t in _FAILURES.get(ip, []) if now - t < WINDOW_SECONDS]


def record_failure() -> None:
    ip = _client_ip()
    with _LOCK:
        now = time.monotonic()
        hits = _prune(ip, now)
        hits.append(now)
        _FAILURES[ip] = hits


def clear_failures() -> None:
    with _LOCK:
        _FAILURES.pop(_client_ip(), None)


def blocked_for() -> float:
    """Seconds remaining in the block window, or 0."""
    ip = _client_ip()
    with _LOCK:
        now = time.monotonic()
        hits = _prune(ip, now)
        _FAILURES[ip] = hits
        if len(hits) < MAX_ATTEMPTS:
            return 0.0
        return max(0.0, BLOCK_SECONDS - (now - hits[0]))


# --------------------------------------------------------------------------
# Session helpers
# --------------------------------------------------------------------------


def csrf_token() -> str:
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def verify_password(candidate: str) -> bool:
    """Constant-time compare against the configured operator password.

    The password is held as plaintext in Config (it comes from the
    environment or a 0600 file), so a constant-time string compare is both
    correct and cheaper than a KDF on every request.
    """
    if not candidate:
        return False
    expected = (Config.OPERATOR_PASSWORD or "").encode("utf-8")
    given = candidate.encode("utf-8")
    return hmac.compare_digest(given, expected)


def is_authenticated() -> bool:
    return bool(session.get("operator"))


# --------------------------------------------------------------------------
# Decorators
# --------------------------------------------------------------------------


def _wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def operator_required(view):
    """Require a valid operator session. HTML gets redirected, API gets 401."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_authenticated():
            if _wants_json():
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


def csrf_protect(view):
    """Require a matching X-CSRF-Token on state-changing requests.

    SameSite=Lax already blocks cross-site cookie sends on POST, but multipart
    uploads are simple requests and cost nothing to protect properly.
    """

    @wraps(view)
    def wrapper(*args, **kwargs):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return view(*args, **kwargs)
        if not is_authenticated():
            return _deny()
        sent = request.headers.get("X-CSRF-Token", "")
        expected = session.get("_csrf", "")
        if not sent or not expected or not hmac.compare_digest(
            sent.encode("utf-8"), expected.encode("utf-8")
        ):
            return _deny()
        return view(*args, **kwargs)

    return wrapper


def _deny():
    if _wants_json():
        return jsonify({"error": "forbidden", "detail": "csrf"}), 403
    return redirect(url_for("login"))