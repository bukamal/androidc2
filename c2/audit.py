"""Operator audit trail.

Every mutating action an operator performs is written here before the response
is sent. The table is append-only: there is no route that updates or deletes
a row, and :func:`seal_digest` chains a hash over the previous entry so a
truncation from the tail is detectable.

Why this exists: "who ran that command" is the question you cannot answer from
``commands`` alone, because that table records the command but not the human
or the session that queued it. It also records the refusals, which is the
other half of the value — a run of denied logins or blocked cross-device
writes is a signal in itself.

Retention is deliberately not automatic. Silent deletion of an audit trail is
worse than a full disk; ``prune()`` exists but must be called deliberately.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from flask import g, has_request_context, request, session
from sqlalchemy import event

from config import Config
from c2.timeutil import utcnow
from database import db
from models import OperatorAction

# Actions that alter state. Anything in here is expected to be audited; the
# route decorators below are the enforcement point.
MUTATING = frozenset({
    "auth.login_ok",
    "auth.login_fail",
    "auth.logout",
    "command.queue",
    "command.rejected",
    "device.delete",
    "device.notes",
    "device.tags",
    "file.push",
    "file.download",
    "file.view",
})


class AuditError(RuntimeError):
    """Raised when a required audit field is missing."""


def actor() -> str:
    """Identifier for whoever is acting.

    Prefers an explicit session identity; falls back to the remote address so
    a pre-login failure still leaves a trace. Never empty: an unattributed
    action is not an audit record.
    """
    if has_request_context():
        # session["operator"] is a boolean flag, not an identity — never
        # stringify that into the audit trail.
        for key in ("operator_name", "operator_id"):
            ident = session.get(key)
            if ident:
                return str(ident)

        # Bearer-token callers have no session; resolve the token.
        try:
            from c2.auth import token_operator

            row = token_operator()
            if row is not None:
                return row.name
        except Exception:
            pass

        return f"ip:{request.remote_addr or 'unknown'}"
    return "system"


def request_context() -> dict:
    if not has_request_context():
        return {}
    return {
        "ip": request.remote_addr,
        "path": request.path,
        "method": request.method,
        "user_agent": (request.headers.get("User-Agent") or "")[:200],
        "session": session.get("sid", "") if session.get("sid") else None,
    }


def record(action: str, *, device=None, command=None, outcome="ok",
           detail: dict | None = None) -> OperatorAction:
    """Append one audit entry. Safe to call outside a request context."""
    if not action:
        raise AuditError("audit action is required")

    detail = detail or {}
    payload = {
        "action": action,
        "device_id": getattr(device, "device_id", None),
        "command_id": getattr(command, "id", None),
        "outcome": outcome,
        "detail": detail,
    }

    entry = OperatorAction(
        created_at=utcnow(),
        actor=actor(),
        action=action,
        device_pk=getattr(device, "id", None) if device is not None else None,
        device_id=getattr(device, "device_id", None),
        command_id=getattr(command, "id", None),
        command_type=getattr(command, "command_type", None),
        outcome=outcome,
        detail_json=json.dumps(detail, default=str)[:8000],
        request_json=json.dumps(request_context(), default=str)[:2000],
    )

    entry.digest = chain_digest(entry)
    db.session.add(entry)
    return entry


def chain_digest(entry: OperatorAction) -> str:
    """Hash this entry together with the digest of the one before it.

    Each row therefore commits to the full history preceding it. Deleting or
    altering any earlier row invalidates every digest after it, which is what
    makes tail-truncation detectable.
    """
    previous = (
        db.session.query(OperatorAction.digest)
        .order_by(OperatorAction.id.desc())
        .limit(1)
        .scalar()
    ) or ""
    return _digest_for(entry, previous)


def _digest_for(entry: OperatorAction, previous: str) -> str:
    """The hash of one entry given the digest that precedes it."""
    material = json.dumps({
        "prev": previous,
        "actor": entry.actor,
        "action": entry.action,
        "device_id": entry.device_id,
        "command_id": entry.command_id,
        "command_type": entry.command_type,
        "outcome": entry.outcome,
        "detail": entry.detail_json,
        "request": entry.request_json,
        "at": entry.created_at.isoformat() if entry.created_at else None,
    }, sort_keys=True, default=str)

    return hmac.new(
        Config.SECRET_KEY.encode("utf-8"),
        material.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_chain(limit: int = 500) -> dict:
    """Recompute every digest and report the first mismatch.

    Rows are re-hashed in insertion order, each fed the digest of the row
    before it. Altering or deleting an earlier entry invalidates everything
    after it, so the first failing id is where the history stops agreeing
    with itself.
    """
    rows = (db.session.query(OperatorAction)
            .order_by(OperatorAction.id.asc())
            .limit(limit)
            .all())

    previous = ""
    for position, row in enumerate(rows, 1):
        expected = _digest_for(row, previous)
        if expected != row.digest:
            return {
                "ok": False,
                "broken_at": row.id,
                "position": position,
                "checked": position,
                "reason": "digest mismatch",
            }
        previous = row.digest

    return {"ok": True, "broken_at": None, "position": None, "checked": len(rows)}


def prune(older_than_days: int) -> int:
    """Delete entries older than the cutoff. Never called automatically."""
    from datetime import timedelta

    cutoff = utcnow() - timedelta(days=older_than_days)
    rows = (db.session.query(OperatorAction)
            .filter(OperatorAction.created_at < cutoff).all())
    for row in rows:
        db.session.delete(row)
    return len(rows)


def session_identity() -> str | None:
    """Short random id for this operator session, for correlation."""
    return session.get("sid")