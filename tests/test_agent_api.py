"""Agent-facing API: shared-key auth and per-device scoping."""

from io import BytesIO

HEADERS = {"X-Api-Key": "test-agent-key"}


def register(client, device_id, **fields):
    body = {"device_id": device_id}
    body.update(fields)
    return client.post("/api/agent/register", json=body, headers=HEADERS)


# ----------------------------------------------------------------- auth

def test_all_agent_endpoints_require_the_key(client):
    cases = [
        ("post", "/api/agent/register", {"device_id": "x"}),
        ("post", "/api/agent/poll", {"device_id": "x"}),
        ("post", "/api/agent/result", {"device_id": "x", "command_id": 1}),
        ("post", "/api/agent/log", {"device_id": "x", "message": "hi"}),
        ("get", "/api/agent/download/a.apk", None),
    ]
    for method, path, body in cases:
        r = client.open(path, method=method.upper(), json=body)
        assert r.status_code == 401, f"{path} -> {r.status_code}"


def test_wrong_key_rejected(client):
    r = client.post("/api/agent/register", json={"device_id": "x"},
                    headers={"X-Api-Key": "wrong"})
    assert r.status_code == 401


def test_key_check_is_not_prefix_based(client):
    r = client.post("/api/agent/register", json={"device_id": "x"},
                    headers={"X-Api-Key": "test-agent-key-extra"})
    assert r.status_code == 401


# ------------------------------------------------------------- scoping

def test_result_cannot_complete_another_devices_command(client, make_device,
                                                        make_command):
    dev_a = make_device("dev-a")
    dev_b = make_device("dev-b")
    cmd_b = make_command(dev_b, "device_info")

    r = client.post("/api/agent/result", headers=HEADERS, json={
        "device_id": "dev-a",
        "command_id": cmd_b,
        "success": True,
        "result": {"stolen": "dev-b data"},
    })
    assert r.status_code == 404

    from database import db
    from models import Command

    with client.application.app_context():
        cmd = db.session.get(Command, cmd_b)
        assert cmd.status == "pending"
        assert cmd.result == ""


def test_result_for_own_command_is_accepted(client, make_device, make_command):
    dev_a = make_device("dev-a")
    cmd_a = make_command(dev_a, "device_info")

    r = client.post("/api/agent/result", headers=HEADERS, json={
        "device_id": "dev-a",
        "command_id": cmd_a,
        "success": True,
        "result": {"model": "Pixel"},
    })
    assert r.status_code == 200

    from database import db
    from models import Command

    with client.application.app_context():
        cmd = db.session.get(Command, cmd_a)
        assert cmd.status == "done"
        assert "Pixel" in cmd.result


def test_result_for_unknown_device_rejected(client, make_device, make_command):
    dev_a = make_device("dev-a")
    cmd_a = make_command(dev_a)
    r = client.post("/api/agent/result", headers=HEADERS, json={
        "device_id": "ghost", "command_id": cmd_a, "success": True,
    })
    assert r.status_code == 404


def test_result_rejects_malformed_command_id(client):
    register(client, "dev-x")
    for bad in [None, "abc", {"a": 1}]:
        r = client.post("/api/agent/result", headers=HEADERS,
                        json={"device_id": "dev-x", "command_id": bad,
                              "success": True})
        assert r.status_code in (400, 404)


def test_poll_only_returns_own_pending_commands(client, make_device, make_command):
    dev_a = make_device("dev-a")
    dev_b = make_device("dev-b")
    cmd_a = make_command(dev_a, "device_info")
    make_command(dev_b, "device_info")

    r = client.post("/api/agent/poll", headers=HEADERS, json={"device_id": "dev-a"})
    assert r.status_code == 200
    ids = [c["id"] for c in r.get_json()["commands"]]
    assert ids == [cmd_a]


def test_poll_for_unknown_device(client):
    r = client.post("/api/agent/poll", headers=HEADERS, json={"device_id": "ghost"})
    assert r.status_code == 404


def test_upload_cannot_claim_another_devices_command(client, make_device,
                                                     make_command):
    dev_a = make_device("dev-a")
    dev_b = make_device("dev-b")
    cmd_b = make_command(dev_b, "screenshot")

    client.post(
        "/api/agent/upload",
        data={"device_id": "dev-a", "category": "screenshot",
              "command_id": str(cmd_b),
              "file": (BytesIO(b"png"), "shot.png")},
        headers=HEADERS,
        content_type="multipart/form-data",
    )

    from database import db
    from models import CapturedFile

    with client.application.app_context():
        cf = CapturedFile.query.filter_by(device_id=dev_a).first()
        assert cf is not None
        assert cf.command_id is None


# ------------------------------------------------------------ hardening

def test_register_coerces_types_safely(client):
    r = register(client, "dev-typed", sdk_int="not-a-number", battery=[1, 2],
                 latitude="abc", longitude=None)
    assert r.status_code == 200

    from database import db
    from models import Device

    with client.application.app_context():
        dev = Device.query.filter_by(device_id="dev-typed").first()
        assert dev.sdk_int is None
        assert dev.battery == 0
        assert dev.latitude == 0.0


def test_log_entry_is_length_capped(client):
    register(client, "dev-log")
    r = client.post("/api/agent/log", headers=HEADERS,
                    json={"device_id": "dev-log", "message": "A" * 100000})
    assert r.status_code == 200

    from database import db
    from models import LogEntry

    with client.application.app_context():
        entry = LogEntry.query.first()
        assert len(entry.message) <= 4096


def test_operator_session_does_not_grant_agent_access(op):
    """An operator cookie must not be usable as an agent key."""
    r = op.post("/api/agent/register", json={"device_id": "dev-a"})
    assert r.status_code == 401


def test_agent_key_does_not_grant_operator_access(client):
    r = client.get("/api/devices", headers=HEADERS)
    assert r.status_code == 401