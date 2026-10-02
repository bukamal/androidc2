"""Realtime bridge for the operator dashboard."""

from flask_socketio import SocketIO, emit, join_room, leave_room

socketio = SocketIO(cors_allowed_origins="*", async_mode="threading")


def init_socketio(app):
    socketio.init_app(app)


def broadcast(event, data, room: str | None = None):
    if room:
        socketio.emit(event, data, namespace="/dashboard", room=room)
    else:
        socketio.emit(event, data, namespace="/dashboard")


@socketio.on("connect", namespace="/dashboard")
def on_connect(auth=None):
    join_room("operators")
    emit("connected", {"ok": True})


@socketio.on("subscribe", namespace="/dashboard")
def on_subscribe(data=None):
    device_id = (data or {}).get("device_id")
    if device_id:
        join_room(f"device_{device_id}")
        emit("subscribed", {"device_id": device_id})


@socketio.on("unsubscribe", namespace="/dashboard")
def on_unsubscribe(data=None):
    device_id = (data or {}).get("device_id")
    if device_id:
        leave_room(f"device_{device_id}")


@socketio.on("subscribe_global", namespace="/dashboard")
def on_subscribe_global(_data=None):
    join_room("all_devices")
    emit("subscribed_global", {"ok": True})
