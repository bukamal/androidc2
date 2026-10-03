"""Realtime bridge for the operator dashboard.

The dashboard is served by this same process, so it is always same-origin.
``cors_allowed_origins`` is therefore left at the default (None): with "*"
any web page the operator happens to visit could open a socket to
``ws://127.0.0.1:5000`` and read every device, file and command live.
"""

from flask_socketio import SocketIO, emit, join_room, leave_room

from c2.auth import is_authenticated

socketio = SocketIO(
    cors_allowed_origins=None,
    async_mode="threading",
    ping_interval=5,      # ping every 5 seconds
    ping_timeout=10,      # timeout after 10 seconds
    logger=False,
)


def init_socketio(app):
    socketio.init_app(app)


def broadcast(event, data, room: str | None = None):
    """Emit to the dashboard namespace, optionally narrowed to a room."""
    if room:
        socketio.emit(event, data, namespace="/dashboard", room=room)
    else:
        socketio.emit(event, data, namespace="/dashboard")


def broadcast_to_device(dev_id: int, event: str, data) -> None:
    """Send to operators watching one device, plus the global feed.

    The dashboard subscribes with the numeric primary key (``dev.id``), so
    rooms are keyed by that — not by the agent-facing device_id string.
    """
    broadcast(event, data, room=f"device_{dev_id}")
    broadcast(event, data, room="all_devices")
    broadcast(event, data)


@socketio.on("connect", namespace="/dashboard")
def on_connect(auth=None):
    if not is_authenticated():
        # Return False rather than raising ConnectionRefusedError: raising
        # from a connect handler breaks the Werkzeug WSGI response cycle
        # ("write() before start_response") and surfaces as a 500.
        return False
    join_room("operators")
    join_room("all_devices")
    emit("connected", {"ok": True})


@socketio.on("subscribe", namespace="/dashboard")
def on_subscribe(data=None):
    if not is_authenticated():
        return
    device_id = (data or {}).get("device_id")
    if device_id is not None:
        join_room(f"device_{device_id}")
        emit("subscribed", {"device_id": device_id})


@socketio.on("unsubscribe", namespace="/dashboard")
def on_unsubscribe(data=None):
    if not is_authenticated():
        return
    device_id = (data or {}).get("device_id")
    if device_id is not None:
        leave_room(f"device_{device_id}")


@socketio.on("subscribe_global", namespace="/dashboard")
def on_subscribe_global(_data=None):
    if not is_authenticated():
        return
    join_room("all_devices")
    emit("subscribed_global", {"ok": True})