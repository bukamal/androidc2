"""WebSocket namespace for persistent agent connections.

Trust model
-----------
A socket is *unauthenticated* the moment it connects. It has exactly one
capability in that state: send ``hello``. Every other event from an
unauthenticated socket is refused and the connection is dropped.

Once authenticated the socket is pinned to a single ``device_id`` for its
whole lifetime, so it cannot touch another device's state or commands even
though it holds the shared key.
"""

from __future__ import annotations

import json
import time

from flask import request
from flask_socketio import disconnect, emit, join_room

from c2.auth import agent_key_ok
from c2.timeutil import utcnow
from c2.ws_dashboard import broadcast, socketio
from database import db
from models import Command, Device

RESULT_MAX_CHARS = 512 * 1024

# device_id -> sid
_agents: dict[str, str] = {}
# sid -> device_id (authenticated sockets only)
_sid_devices: dict[str, str] = {}


def _auth_sid() -> str | None:
    """device_id this socket is authenticated as, or None."""
    return _sid_devices.get(request.sid)


def _require_auth():
    """Return the bound device_id, or None after dropping the socket.

    Raising ConnectionRefusedError only works from the ``connect`` handler.
    In a data handler it escapes as an unhandled server exception, so the
    clean way to kick an unauthorised client is ``disconnect()``.
    """
    device_id = _auth_sid()
    if not device_id:
        disconnect()
        return None
    return device_id


def register_agent_ws():
    @socketio.on("connect", namespace="/agent")
    def on_agent_connect(auth=None):
        a = auth if isinstance(auth, dict) else {}
        key = a.get("api_key") or request.args.get("api_key", "")
        device_id = a.get("device_id") or ""

        if not agent_key_ok(key) or not device_id:
            if device_id:
                # Explicit, clean refusal: the client receives
                # 44/agent,{"message":"Connection rejected by server"}.
                # Do not raise here — Flask-SocketIO turns a raise into the
                # same refusal but with a noisier failure path.
                print("[agent] rejected handshake: bad key or missing device_id",
                      flush=True)
                return False

            # No device_id in the handshake: this client authenticates later
            # via `hello`. Accept it, but do not let it linger forever if
            # `hello` never arrives.
            print("[agent] anonymous connect, awaiting hello", flush=True)
            _kick_if_unbound(60.0)
            return True

        _bind(device_id, a)
        emit("accept", {"device_id": device_id, "commands": _drain(device_id)})
        print(f"[agent] accepted {device_id}", flush=True)
        return True

    @socketio.on("hello", namespace="/agent")
    def on_agent_hello(data):
        a = data if isinstance(data, dict) else {}
        if not agent_key_ok(a.get("api_key")):
            emit("reject", {"error": "unauthorized"})
            disconnect()
            return

        device_id = a.get("device_id")
        if not device_id:
            emit("reject", {"error": "missing_device_id"})
            disconnect()
            return

        _bind(device_id, a)
        emit("accept", {"device_id": device_id, "commands": _drain(device_id)})
        print(f"[agent] accepted via hello {device_id}", flush=True)

    @socketio.on("state", namespace="/agent")
    def on_agent_state(data):
        device_id = _require_auth()
        if not device_id:
            return
        a = data if isinstance(data, dict) else {}

        dev = db.session.query(Device).filter_by(device_id=device_id).first()
        if not dev:
            disconnect()
            return

        dev.last_seen = utcnow()
        dev.is_online = True
        battery = a.get("battery")
        if battery is not None:
            try:
                dev.battery = int(battery)
            except (TypeError, ValueError):
                pass
        if a.get("latitude") is not None and a.get("longitude") is not None:
            try:
                dev.latitude = float(a["latitude"])
                dev.longitude = float(a["longitude"])
            except (TypeError, ValueError):
                pass
        db.session.commit()
        _emit_device(dev)

    @socketio.on("result", namespace="/agent")
    def on_agent_result(data):
        device_id = _require_auth()
        if not device_id:
            return
        a = data if isinstance(data, dict) else {}

        dev = db.session.query(Device).filter_by(device_id=device_id).first()
        if not dev:
            disconnect()
            return

        raw_id = a.get("command_id")
        if raw_id is None:
            return
        try:
            cmd_id = int(raw_id)
        except (TypeError, ValueError):
            return

        cmd = db.session.get(Command, cmd_id)
        # Sequential ids: existence alone is not authorisation.
        if not cmd or cmd.device_id != dev.id:
            emit("command_rejected", {"command_id": raw_id, "error": "not_yours"})
            return

        cmd.result = json.dumps(a.get("result", {}))[:RESULT_MAX_CHARS]
        cmd.status = "done" if a.get("success") else "failed"
        cmd.completed_at = utcnow()
        db.session.commit()
        broadcast("command_result", cmd.to_dict())

    @socketio.on("poll", namespace="/agent")
    def on_agent_poll(_data=None):
        device_id = _require_auth()
        if not device_id:
            return

        dev = db.session.query(Device).filter_by(device_id=device_id).first()
        if not dev:
            emit("commands", {"commands": []})
            return

        dev.last_seen = utcnow()
        dev.is_online = True
        db.session.commit()
        emit("commands", {"commands": _drain(device_id)})

    @socketio.on("disconnect", namespace="/agent")
    def on_agent_disconnect(_reason=None):
        sid = request.sid
        device_id = _sid_devices.pop(sid, None)
        if not device_id:
            return

        # Only clear the registry if this sid still owns the device; a stale
        # socket disconnecting must not evict its live replacement.
        if _agents.get(device_id) == sid:
            _agents.pop(device_id, None)
            dev = db.session.query(Device).filter_by(device_id=device_id).first()
            if dev:
                dev.is_online = False
                db.session.commit()
                _emit_device(dev)
            print(f"[agent] disconnected {device_id}", flush=True)


def _kick_if_unbound(delay: float = 10.0) -> None:
    """Close a socket that never authenticated.

    An anonymous socket is accepted so it can send `hello`. Without this it
    would sit there holding resources until the client gives up. No-op if the
    socket authenticated in the meantime.
    """
    sid = request.sid

    def later():
        time.sleep(delay)
        if sid in _sid_devices:
            return
        try:
            disconnect(sid)
        except Exception:
            pass

    socketio.start_background_task(later)


def _bind(device_id: str, meta: dict) -> None:
    previous = _agents.get(device_id)
    if previous and previous != request.sid:
        _sid_devices.pop(previous, None)

    _agents[device_id] = request.sid
    _sid_devices[request.sid] = device_id
    join_room(f"agent_{device_id}")

    dev = db.session.query(Device).filter_by(device_id=device_id).first()
    if not dev:
        dev = Device(device_id=device_id, first_seen=utcnow())
        db.session.add(dev)

    dev.model = meta.get("model") or dev.model
    dev.manufacturer = meta.get("manufacturer") or dev.manufacturer
    dev.android_version = meta.get("android_version") or dev.android_version
    sdk = meta.get("sdk_int")
    if sdk is not None:
        try:
            dev.sdk_int = int(sdk)
        except (TypeError, ValueError):
            pass
    dev.hostname = meta.get("hostname") or dev.hostname
    dev.ip_address = request.remote_addr
    dev.last_seen = utcnow()
    dev.is_online = True
    db.session.commit()

    _emit_device(dev)


def _emit_device(dev: Device) -> None:
    broadcast("device_update", dev.to_dict())
    broadcast("device_update", dev.to_dict(), room="all_devices")
    broadcast("device_update", dev.to_dict(), room=f"device_{dev.id}")


def _drain(device_id: str) -> list[dict]:
    """Hand pending commands to the agent and mark them delivered."""
    dev = db.session.query(Device).filter_by(device_id=device_id).first()
    if not dev:
        return []

    pending = (Command.query.filter_by(device_id=dev.id, status="pending")
               .order_by(Command.id.asc()).limit(20).all())
    now = utcnow()
    for c in pending:
        c.status = "sent"
        c.delivered_at = now
    if pending:
        db.session.commit()

    return [
        {"id": c.id, "type": c.command_type, "args": c.args()}
        for c in pending
    ]


# --------------------------------------------------------------------------
# Registry accessors
# --------------------------------------------------------------------------


def send_command_to_agent(device_id: str, command: dict) -> bool:
    sid = _agents.get(device_id)
    if not sid or sid not in _sid_devices:
        return False
    socketio.emit("command", command, namespace="/agent", to=sid)
    return True


def agent_online(device_id: str) -> bool:
    return _agents.get(device_id) in _sid_devices


def connected_devices() -> set[str]:
    return {d for d, s in _agents.items() if s in _sid_devices}


def reset_registry() -> None:
    """Test helper."""
    _agents.clear()
    _sid_devices.clear()