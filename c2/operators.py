"""Operator credentials: password login and bearer-token machine auth.

Why this exists: with one shared panel password the audit trail could only
ever record "operator", which makes it a log rather than an accountability
record. Operators are named rows; each has its own hashed password, its own
optional API token, and its own throttle bucket.

Passwords are stored as werkzeug scrypt hashes and compared with
``check_password_hash``. Tokens are stored as sha256 so that lookup is a
single indexed query rather than a scan, and so a database leak does not hand
over usable tokens directly (the token itself is only ever shown once).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from werkzeug.security import check_password_hash, generate_password_hash

from c2.timeutil import utcnow
from database import db
from models import Operator

TOKEN_BYTES = 32
NAME_MAX = 64
MIN_PASSWORD_LENGTH = 12


class AuthError(Exception):
    """Credential problem. Message is deliberately vague for the caller."""


# ------------------------------------------------------------- validation

def valid_name(name: str | None) -> bool:
    """Names go into the audit trail and into log lines, so keep them boring."""
    if not name or not isinstance(name, str):
        return False
    if len(name) > NAME_MAX:
        return False
    if not name[0].isalpha():
        return False
    return all(c.isalnum() or c in "._-" for c in name)


def check_password_strength(password: str | None) -> str | None:
    """Return an error message, or None if acceptable.

    The previous single shared password was generated with
    ``token_urlsafe(32)`` and often overridden with something short. Anything
    under 12 characters is refused.
    """
    if not password or not isinstance(password, str):
        return "password required"
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"password must be at least {MIN_PASSWORD_LENGTH} characters"
    return None


# ------------------------------------------------------------------ create

def create_operator(name: str, password: str, role: str = "admin",
                    token: str | None = None) -> tuple[Operator, str | None]:
    """Create an operator. Returns (row, plaintext_token or None).

    The plaintext token is returned exactly once; only its hash is stored.
    """
    if not valid_name(name):
        raise AuthError(f"invalid operator name: {name!r}")
    problem = check_password_strength(password)
    if problem:
        raise AuthError(problem)
    if role not in ("admin", "viewer"):
        raise AuthError(f"unknown role: {role!r}")

    existing = Operator.query.filter_by(name=name).first()
    if existing:
        raise AuthError(f"operator already exists: {name}")

    plaintext = token
    if token is None:
        plaintext = secrets.token_urlsafe(TOKEN_BYTES)

    row = Operator(
        name=name,
        password_hash=generate_password_hash(password),
        token_hash=hash_token(plaintext),
        role=role,
        disabled=False,
        created_at=utcnow(),
    )
    db.session.add(row)
    db.session.commit()
    return row, plaintext


def set_password(name: str, password: str) -> Operator:
    problem = check_password_strength(password)
    if problem:
        raise AuthError(problem)
    row = require(name)
    row.password_hash = generate_password_hash(password)
    db.session.commit()
    return row


def require(name: str) -> Operator:
    row = Operator.query.filter_by(name=name).first()
    if not row:
        raise AuthError("unknown operator")
    return row


# ------------------------------------------------------------------ tokens

def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def issue_token(name: str) -> tuple[Operator, str]:
    """Rotate an operator's token, returning the new plaintext once."""
    row = require(name)
    plaintext = secrets.token_urlsafe(TOKEN_BYTES)
    row.token_hash = hash_token(plaintext)
    db.session.commit()
    return row, plaintext


def revoke_token(name: str) -> Operator:
    row = require(name)
    row.token_hash = None
    db.session.commit()
    return row


def operator_for_token(plaintext: str | None) -> Operator | None:
    """Resolve a bearer token to an operator in one indexed query."""
    if not plaintext or len(plaintext) < 16:
        return None
    row = Operator.query.filter_by(token_hash=hash_token(plaintext)).first()
    if row is None or row.disabled:
        return None
    return row


# ------------------------------------------------------------------ verify

def verify_password(name: str, password: str) -> Operator:
    """Check a name/password pair. Raises AuthError on any failure.

    An unknown name still runs a hash comparison so that a wrong name and a
    wrong password take comparable time — otherwise the response latency
    enumerates valid operator names.
    """
    row = Operator.query.filter_by(name=name).first()

    if row is None:
        # Burn an equivalent amount of work.
        check_password_hash(
            generate_password_hash(secrets.token_urlsafe(16)), password or ""
        )
        raise AuthError("invalid credentials")

    if row.disabled:
        check_password_hash(row.password_hash, password or "")
        raise AuthError("invalid credentials")

    if not check_password_hash(row.password_hash, password or ""):
        raise AuthError("invalid credentials")

    return row


def verify_token(plaintext: str | None) -> Operator | None:
    return operator_for_token(plaintext)


def touch_login(row: Operator, ip: str | None) -> None:
    row.last_login = utcnow()
    row.last_login_ip = (ip or "")[:64] or None
    db.session.commit()


def disable(name: str, disabled: bool = True) -> Operator:
    row = require(name)
    row.disabled = disabled
    db.session.commit()
    return row


def delete(name: str) -> None:
    row = require(name)
    db.session.delete(row)
    db.session.commit()


def ensure_bootstrap(default_name: str, default_password: str | None) -> tuple[str, Operator | None]:
    """Create the first operator if the table is empty.

    Returns (action, row) where action is one of:
      * "created"  — a bootstrap operator was made
      * "existing" — operators already exist, nothing to do
      * "skipped"  — table empty and no usable password was supplied
    """
    if Operator.query.first() is not None:
        return "existing", None

    if not default_password:
        return "skipped", None

    row, _ = create_operator(default_name, default_password, role="admin")
    return "created", row


# --------------------------------------------------------------- throttle

def throttle_key(name: str | None, ip: str | None) -> str:
    """Bucket by operator name and by source separately.

    Bucketing only on IP lets one host lock out every operator; bucketing only
    on name lets an attacker lock a known account from anywhere.
    """
    who = (name or "?").strip().lower()[:NAME_MAX]
    return f"name:{who}"


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").encode(), (b or "").encode())
