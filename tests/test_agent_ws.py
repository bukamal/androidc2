"""WebSocket namespace: handshake auth and per-device pinning."""

import pytest

from conftest import API_KEY
from c2.agent_ws import agent_online, connected_devices, send_command_to_agent
from c2.ws_dashboard import socketio
from database import db
from models import Command, Device

NS = "/agent"


@pytest.fixture(scope="session")
def ws_app(app):
    """Same app instance the rest of the suite uses.

    A second create_app() would re-register the Socket.IO handlers and leave
    two Flask apps fighting over one sqlite file.
    """
    return app


def agent(app, device_id=None, api_key=API_KEY):
    auth = {}
    if device_id:
        auth["device_id"] = device_id
    if api_key is not None:
        auth["api_key"] = api_key
    return socketio.test_client(app, namespace=NS, auth=auth or None)


def names(client):
    return [e["name"] for e in client.get_received(namespace=NS)]


def close(client):
    try:
        if client.is_connected(NS):
            client.disconnect(namespace=NS)
    except Exception:
        pass


def seed_device(app, device_id):
    with app.app_context():
        dev = Device.query.filter_by(device_id=device_id).first()
        if dev is None:
            dev = Device(device_id=device_id)
            db.session.add(dev)
            db.session.commit()
        return dev.id


def seed_command(app, device_id, status="pending"):
    dev_pk = seed_device(app, device_id)
    with app.app_context():
        cmd = Command(device_id=dev_pk, command_type="device_info",
                      payload="{}", status=status)
        db.session.add(cmd)
        db.session.commit()
        return cmd.id


# ------------------------------------------------------------- handshake

def test_handshake_auth_registers_device(ws_app):
    c = agent(ws_app, "dev-ws-1")
    assert c.is_connected(NS)
    assert agent_online("dev-ws-1")
    assert "accept" in names(c)
    close(c)


def test_handshake_with_wrong_key_is_refused(ws_app):
    c = agent(ws_app, "dev-bad", api_key="not-the-key")
    assert not c.is_connected(NS)
    assert not agent_online("dev-bad")


def test_key_prefix_is_not_accepted(ws_app):
    c = agent(ws_app, "dev-prefix", api_key=API_KEY + "x")
    assert not c.is_connected(NS)


def test_anonymous_socket_may_only_say_hello(ws_app):
    c = agent(ws_app, device_id=None, api_key=None)
    assert c.is_connected(NS)
    assert connected_devices() == set()

    # Any data event before hello is refused *and* kicks the socket.
    c.emit("state", {"device_id": "dev-ws-1", "battery": 99}, namespace=NS)
    assert not c.is_connected(NS)

    with ws_app.app_context():
        assert Device.query.filter_by(device_id="dev-ws-1").first() is None
    close(c)


def test_anonymous_socket_cannot_poll(ws_app):
    c = agent(ws_app, device_id=None, api_key=None)
    c.emit("poll", {"device_id": "dev-ws-1"}, namespace=NS)

    assert not c.is_connected(NS)
    with ws_app.app_context():
        assert Device.query.filter_by(device_id="dev-ws-1").first() is None
    close(c)


def test_hello_authenticates_an_anonymous_socket(ws_app):
    c = agent(ws_app, device_id=None, api_key=None)
    c.emit("hello", {"api_key": API_KEY, "device_id": "dev-hello"},
           namespace=NS)

    assert agent_online("dev-hello")
    assert "accept" in names(c)
    close(c)


def test_hello_with_bad_key_is_refused(ws_app):
    c = agent(ws_app, device_id=None, api_key=None)
    c.emit("hello", {"api_key": "nope", "device_id": "dev-x"}, namespace=NS)

    assert not agent_online("dev-x")
    assert not c.is_connected(NS)


def test_hello_without_device_id_is_refused(ws_app):
    c = agent(ws_app, device_id=None, api_key=None)
    c.emit("hello", {"api_key": API_KEY}, namespace=NS)

    assert connected_devices() == set()
    assert not c.is_connected(NS)


# --------------------------------------------------------------- scoping

def test_authenticated_socket_cannot_touch_another_device(ws_app):
    victim = agent(ws_app, "dev-victim")
    attacker = agent(ws_app, "dev-attacker")
    cmd_id = seed_command(ws_app, "dev-victim", status="sent")

    attacker.emit("result", {
        "command_id": cmd_id,
        "success": True,
        "result": {"stolen": True},
    }, namespace=NS)

    with ws_app.app_context():
        cmd = db.session.get(Command, cmd_id)
        assert cmd.status == "sent"
        assert cmd.result == ""

    close(victim)
    close(attacker)


def test_authenticated_socket_can_complete_its_own_command(ws_app):
    c = agent(ws_app, "dev-owner")
    cmd_id = seed_command(ws_app, "dev-owner", status="sent")

    c.emit("result", {"command_id": cmd_id, "success": True,
                      "result": {"model": "Pixel"}}, namespace=NS)

    with ws_app.app_context():
        cmd = db.session.get(Command, cmd_id)
        assert cmd.status == "done"
        assert "Pixel" in cmd.result
    close(c)


def test_state_updates_only_own_device(ws_app):
    c = agent(ws_app, "dev-state")
    c.emit("state", {"battery": 42, "latitude": 24.7, "longitude": 46.6},
           namespace=NS)

    with ws_app.app_context():
        dev = Device.query.filter_by(device_id="dev-state").first()
        assert dev.battery == 42
        assert abs(dev.latitude - 24.7) < 1e-6
        assert dev.is_online is True
    close(c)


def test_poll_drains_only_own_queue(ws_app):
    other = agent(ws_app, "dev-other")
    mine = agent(ws_app, "dev-mine")

    seed_command(ws_app, "dev-other")
    seed_command(ws_app, "dev-mine")
    mine_cmd = _latest_command(ws_app, "dev-mine")
    other_cmd = _latest_command(ws_app, "dev-other")

    mine.emit("poll", {}, namespace=NS)
    delivered = [d for e in mine.get_received(namespace=NS)
                 if e["name"] == "commands"
                 for d in e["args"][0]["commands"]]

    assert [d["id"] for d in delivered] == [mine_cmd]
    with ws_app.app_context():
        assert db.session.get(Command, other_cmd).status == "pending"
        assert db.session.get(Command, mine_cmd).status == "sent"

    close(other)
    close(mine)


def _latest_command(app, device_id):
    with app.app_context():
        dev = Device.query.filter_by(device_id=device_id).first()
        return (Command.query.filter_by(device_id=dev.id)
                .order_by(Command.id.desc()).first().id)


# ------------------------------------------------------------- lifecycle

def test_command_is_pushed_to_the_live_socket(ws_app):
    c = agent(ws_app, "dev-live")
    assert send_command_to_agent("dev-live", {"id": 1, "type": "device_info"})

    pushed = [e for e in c.get_received(namespace=NS) if e["name"] == "command"]
    assert pushed and pushed[0]["args"][0]["id"] == 1
    close(c)

    assert not send_command_to_agent("dev-live", {"id": 2})


def test_disconnect_marks_device_offline(ws_app):
    c = agent(ws_app, "dev-bye")
    with ws_app.app_context():
        assert Device.query.filter_by(device_id="dev-bye").first().is_online is True

    c.disconnect(namespace=NS)
    with ws_app.app_context():
        assert Device.query.filter_by(device_id="dev-bye").first().is_online is False


def test_reconnect_evicts_the_stale_sid(ws_app):
    first = agent(ws_app, "dev-dup")
    agent(ws_app, "dev-dup")
    assert agent_online("dev-dup")

    # The old socket dropping must not take the live replacement offline.
    first.disconnect(namespace=NS)
    with ws_app.app_context():
        dev = Device.query.filter_by(device_id="dev-dup").first()
        assert dev.is_online is True


def test_dashboard_namespace_requires_operator_session(ws_app):
    anon = socketio.test_client(ws_app, namespace="/dashboard")
    assert not anon.is_connected("/dashboard")