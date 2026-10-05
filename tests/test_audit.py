"""Operator audit trail.

Two properties matter here and are tested directly:

1. Every mutating operator action leaves a row, including refusals.
2. The trail is append-only and tamper-evident — re-hashing the chain must
   detect an edit or a deletion, and no API route may mutate an entry.
"""

import json
from datetime import timedelta

import pytest

from c2 import audit
from c2.audit import record, verify_chain
from c2.timeutil import utcnow
from conftest import OPERATOR_NAME, OPERATOR_PASSWORD
from database import db
from models import Command, Device, OperatorAction

HEAD = {"X-Api-Key": "test-agent-key"}


def login(client):
    from conftest import login

    return login(client)


def entries(app):
    with app.app_context():
        return OperatorAction.query.order_by(OperatorAction.id.asc()).all()


def actions(app):
    with app.app_context():
        return [a.action for a in
                OperatorAction.query.order_by(OperatorAction.id.asc()).all()]


# ------------------------------------------------------------------ login

def test_successful_login_is_recorded(client):
    login(client)
    assert "auth.login_ok" in actions(client.application)


def test_failed_login_is_recorded(client):
    client.post("/login", data={"name": OPERATOR_NAME, "password": "wrong"})
    rows = entries(client.application)
    assert [r.action for r in rows] == ["auth.login_fail"]
    assert rows[0].outcome == "denied"
    assert rows[0].actor.startswith("ip:"), \
        "a pre-login failure has no session identity, so it records the source"


def test_throttled_login_is_recorded(client):
    for _ in range(5):
        client.post("/login", data={"name": OPERATOR_NAME, "password": "wrong"})
    client.post("/login", data={"name": OPERATOR_NAME, "password": OPERATOR_PASSWORD})
    assert "auth.login_throttled" in actions(client.application)


def test_logout_is_recorded(client):
    op = login(client)
    op.post("/logout")
    assert "auth.logout" in actions(client.application)


def test_login_audit_does_not_store_the_password(client):
    client.post("/login", data={"name": OPERATOR_NAME, "password": "hunter2"})
    blob = json.dumps([r.detail_json for r in entries(client.application)])
    assert "hunter2" not in blob
    assert OPERATOR_PASSWORD not in blob


# --------------------------------------------------------------- commands

def test_queued_command_is_recorded_with_args(client, make_device):
    dev_id = make_device("dev-audit")
    op = login(client)
    op.post(f"/api/device/{dev_id}/command",
            json={"type": "shell", "args": {"cmd": "id"}})

    rows = [r for r in entries(client.application) if r.action == "command.queue"]
    assert len(rows) == 1
    entry = rows[0]
    assert entry.device_id == "dev-audit"
    assert entry.command_type == "shell"
    assert entry.command_id is not None
    assert entry.detail()["args"] == {"cmd": "id"}
    assert entry.actor == OPERATOR_NAME, \
        "attribution must be the named operator, not a generic label"


def test_unsupported_command_is_recorded_as_denied(client, make_device):
    dev_id = make_device("dev-caps")
    client.post("/api/agent/register", headers=HEAD,
                json={"device_id": "dev-caps", "capabilities": ["device_info"]})
    op = login(client)
    r = op.post(f"/api/device/{dev_id}/command", json={"type": "inject_payload"})

    assert r.status_code == 409
    rows = [x for x in entries(client.application) if x.action == "command.rejected"]
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].detail()["type"] == "inject_payload"


def test_audit_records_only_the_first_20_are_not_lost(client, make_device):
    dev_id = make_device("dev-many")
    op = login(client)
    for _ in range(3):
        op.post(f"/api/device/{dev_id}/command", json={"type": "device_info"})
    assert actions(client.application).count("command.queue") == 3


# ---------------------------------------------------------------- device

def test_device_delete_is_recorded(client, make_device):
    dev_id = make_device("dev-del")
    op = login(client)
    op.delete(f"/api/device/{dev_id}")

    rows = [r for r in entries(client.application) if r.action == "device.delete"]
    assert len(rows) == 1
    # device_id is denormalised so the row stays readable after deletion.
    assert rows[0].device_id == "dev-del"
    assert rows[0].device_pk == dev_id


def test_notes_and_tags_are_recorded(client, make_device):
    dev_id = make_device("dev-meta")
    op = login(client)
    op.post(f"/api/device/{dev_id}/notes", json={"notes": "watch this one"})
    op.post(f"/api/device/{dev_id}/tags", json={"tags": ["kex", "test"]})

    recorded = {r.action for r in entries(client.application)}
    assert {"device.notes", "device.tags"} <= recorded


def test_file_download_is_recorded(client, make_device):
    import io

    make_device("dev-files")
    client.post("/api/agent/upload", headers=HEAD, data={
        "device_id": "dev-files",
        "file": (io.BytesIO(b"payload"), "loot.bin"),
    }, content_type="multipart/form-data")

    with client.application.app_context():
        from models import CapturedFile
        file_id = CapturedFile.query.first().id

    op = login(client)
    op.get(f"/api/file/{file_id}/raw")

    rows = [r for r in entries(client.application) if r.action == "file.view"]
    assert len(rows) == 1
    assert rows[0].detail()["filename"] == "loot.bin"


def test_file_push_is_recorded(client, make_device):
    import io

    dev_id = make_device("dev-push")
    op = login(client)
    op.post(f"/api/device/{dev_id}/upload_to_agent", data={
        "file": (io.BytesIO(b"APK-BYTES"), "../../evil name.apk"),
    }, content_type="multipart/form-data")

    rows = [r for r in entries(client.application) if r.action == "file.push"]
    assert len(rows) == 1
    assert rows[0].detail()["filename"] == "../../evil name.apk", \
        "the audit records what was submitted, not the sanitised name"
    assert rows[0].command_type == "install_apk"


# ------------------------------------------------------------ read access

def test_audit_requires_a_session(client, make_device):
    assert client.get("/api/audit").status_code == 401


def test_audit_is_readable_when_authenticated(client):
    login(client)
    r = client.get("/api/audit")
    assert r.status_code == 200
    body = r.get_json()
    assert "entries" in body and "total" in body
    assert body["total"] >= 1


def test_audit_entries_carry_attribution(client, make_device):
    dev_id = make_device("dev-attr")
    op = login(client)
    op.post(f"/api/device/{dev_id}/command", json={"type": "device_info"})

    entry = next(e for e in op.get("/api/audit").get_json()["entries"]
                 if e["action"] == "command.queue")
    assert entry["actor"] == OPERATOR_NAME
    assert entry["actor"] != "True", "the session flag is not an identity"
    assert entry["device_id"] == "dev-attr"
    assert entry["command_type"] == "device_info"
    assert entry["request"]["ip"] == "127.0.0.1"
    assert entry["digest"]


# --------------------------------------------------------------- filters

def test_filter_by_action(client, make_device):
    dev_id = make_device("dev-f")
    op = login(client)
    op.post(f"/api/device/{dev_id}/notes", json={"notes": "x"})
    op.post(f"/api/device/{dev_id}/command", json={"type": "device_info"})

    only = op.get("/api/audit?action=command.queue").get_json()
    assert {e["action"] for e in only["entries"]} == {"command.queue"}


def test_filter_by_device_and_outcome(client, make_device):
    a = make_device("dev-1")
    b = make_device("dev-2")
    op = login(client)
    op.post(f"/api/device/{a}/notes", json={"notes": "x"})
    op.post(f"/api/device/{b}/command", json={"type": "device_info"})

    assert op.get("/api/audit?device_id=dev-1").get_json()["total"] >= 1
    denied = op.get("/api/audit?outcome=denied").get_json()
    assert all(e["outcome"] == "denied" for e in denied["entries"])


def test_pagination(client, make_device):
    dev_id = make_device("dev-page")
    op = login(client)
    for _ in range(5):
        op.post(f"/api/device/{dev_id}/command", json={"type": "device_info"})

    page1 = op.get("/api/audit?limit=2&offset=0").get_json()
    page2 = op.get("/api/audit?limit=2&offset=2").get_json()
    assert len(page1["entries"]) == 2
    assert page1["total"] >= 5
    ids1 = [e["id"] for e in page1["entries"]]
    ids2 = [e["id"] for e in page2["entries"]]
    assert not set(ids1) & set(ids2), "pages must not overlap"
    assert ids1[0] > ids2[0], "newest first"


def test_limit_is_capped(client):
    op = login(client)
    assert op.get("/api/audit?limit=99999").get_json()["limit"] == 500


def test_bad_paging_params_are_rejected(client):
    op = login(client)
    assert op.get("/api/audit?limit=abc").status_code == 400
    assert op.get("/api/audit?offset=abc").status_code == 400
    assert op.get("/api/audit?since=not-a-date").status_code == 400


def test_actions_and_actors_listing(client, make_device):
    dev_id = make_device("dev-list")
    op = login(client)
    op.post(f"/api/device/{dev_id}/notes", json={"notes": "x"})

    body = op.get("/api/audit/actions").get_json()
    assert "device.notes" in body["actions"]
    assert OPERATOR_NAME in body["actors"]


def test_since_filter(client, make_device):
    dev_id = make_device("dev-since")
    op = login(client)
    op.post(f"/api/device/{dev_id}/notes", json={"notes": "x"})

    future = (utcnow() + timedelta(hours=1)).isoformat()
    assert op.get(f"/api/audit?since={future}").get_json()["total"] == 0


# -------------------------------------------------------- append-only-ness

def test_no_route_can_modify_or_delete_an_entry(client, make_device):
    """GET-only by construction: no mutation verb is registered."""
    op = login(client)
    with client.application.app_context():
        entry_id = OperatorAction.query.first().id

    for method in ("POST", "PUT", "PATCH", "DELETE"):
        r = op.open(f"/api/audit?action=command.queue", method=method,
                    json={"actor": "forged"})
        assert r.status_code == 405, f"{method} must not be accepted"

    with client.application.app_context():
        row = db.session.get(OperatorAction, entry_id)
        assert row is not None
        assert row.actor != "forged"


def test_model_exposes_no_mutating_helper():
    for attr in ("update", "delete", "touch", "merge", "set_actor"):
        assert not hasattr(OperatorAction, attr), \
            f"OperatorAction.{attr} would defeat append-only"


# ---------------------------------------------------------- digest chain

def test_chain_verifies_on_untouched_log(client, make_device):
    dev_id = make_device("dev-chain")
    op = login(client)
    for i in range(4):
        op.post(f"/api/device/{dev_id}/command", json={"type": "device_info"})

    assert verify_chain()["ok"] is True
    assert op.get("/api/audit/verify").status_code == 200


def test_chain_detects_an_edited_row(client, make_device):
    dev_id = make_device("dev-edit")
    op = login(client)
    op.post(f"/api/device/{dev_id}/command", json={"type": "device_info"})

    with client.application.app_context():
        victim = (OperatorAction.query
                  .filter_by(action="command.queue").first())
        victim.detail_json = json.dumps({"args": {"cmd": "whoops"}})
        db.session.commit()

    result = verify_chain()
    assert result["ok"] is False
    assert result["broken_at"] is not None


def test_chain_detects_a_deleted_row(client, make_device):
    dev_id = make_device("dev-delrow")
    op = login(client)
    for _ in range(3):
        op.post(f"/api/device/{dev_id}/command", json={"type": "device_info"})

    with client.application.app_context():
        rows = (OperatorAction.query
                .filter_by(action="command.queue")
                .order_by(OperatorAction.id.asc()).all())
        db.session.delete(rows[0])
        db.session.commit()

    assert verify_chain()["ok"] is False


def test_chain_detects_a_rewritten_actor(client, make_device):
    dev_id = make_device("dev-actor")
    op = login(client)
    op.post(f"/api/device/{dev_id}/command", json={"type": "device_info"})

    with client.application.app_context():
        entry = OperatorAction.query.filter_by(action="command.queue").first()
        entry.actor = "someone-else"
        db.session.commit()

    assert verify_chain()["ok"] is False


def test_digest_is_keyed_so_it_cannot_be_recomputed_by_hand(client):
    """Two rows with identical content still get different digests, because
    each commits to its predecessor."""
    with client.application.app_context():
        dev = Device.query.first() or Device(device_id="chain-dev")
        db.session.add(dev)
        db.session.commit()

        first = record("test.action", device=dev, detail={"same": 1})
        db.session.commit()
        second = record("test.action", device=dev, detail={"same": 1})
        db.session.commit()

        assert first.digest != second.digest
        assert len(first.digest) == 64


def test_prune_is_not_automatic(client, make_device):
    """Old entries survive a normal request cycle."""
    make_device("dev-keep")
    op = login(client)
    with client.application.app_context():
        old = record("test.old", detail={"age": "ancient"})
        old.created_at = utcnow() - timedelta(days=3650)
        db.session.commit()

        op.get("/api/devices")
        assert db.session.get(OperatorAction, old.id) is not None

        removed = audit.prune(older_than_days=365)
        db.session.commit()
        assert removed == 1
        assert db.session.get(OperatorAction, old.id) is None


def test_record_outside_request_context(app):
    with app.app_context():
        entry = record("system.tick", detail={"ok": True})
        db.session.commit()
        assert entry.actor == "system"
        assert entry.device_id is None