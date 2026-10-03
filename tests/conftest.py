"""Shared fixtures.

Environment is pinned before anything imports ``config`` so the suite never
touches the real database, the real uploads tree, or the real secrets.
"""

import os
import shutil
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="androidc2-tests-")

os.environ["C2_SECRET"] = "test-secret-key"
os.environ["C2_API_KEY"] = "test-agent-key"
os.environ["C2_OPERATOR_PASSWORD"] = "test-operator-pass"
os.environ["C2_DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["C2_UPLOAD_FOLDER"] = os.path.join(_TMP, "data", "uploads")
os.environ["C2_LEGACY_UPLOAD_FOLDER"] = os.path.join(_TMP, "no-such-legacy-dir")
os.environ["C2_DB_URI"] = "sqlite:///" + os.path.join(_TMP, "test.db")
os.environ["C2_HOST"] = "127.0.0.1"

API_KEY = "test-agent-key"
OPERATOR_PASSWORD = "test-operator-pass"

import sys  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from c2 import auth  # noqa: E402
from c2.agent_ws import reset_registry  # noqa: E402
from c2.ws_dashboard import socketio  # noqa: E402
from database import db  # noqa: E402
from models import Device  # noqa: E402


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture(scope="session")
def app():
    application = create_app(start_background=False)
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    return application


@pytest.fixture(autouse=True)
def clean_db(app):
    reset_registry()
    auth._FAILURES.clear()
    with app.app_context():
        db.drop_all()
        db.create_all()
        yield
        db.session.rollback()
        db.session.remove()
    reset_registry()
    auth._FAILURES.clear()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def socket_client(app):
    return socketio.test_client(app, namespace="/agent")


@pytest.fixture
def anon(client):
    """Unauthenticated client."""
    return client


@pytest.fixture
def op(client):
    """Client with a valid operator session and a CSRF token."""
    client.post("/login", data={"password": OPERATOR_PASSWORD})
    token = client.get("/api/session").get_json()["csrf_token"]
    client.environ_base["HTTP_X_CSRF_TOKEN"] = token
    return client


@pytest.fixture
def make_device(app):
    def _make(device_id="dev-alpha", **fields):
        with app.app_context():
            dev = Device(device_id=device_id, **fields)
            db.session.add(dev)
            db.session.commit()
            return dev.id
    return _make


@pytest.fixture
def make_command(app):
    def _make(device_pk, ctype="device_info", args=None, status="pending"):
        from c2.command_builder import build_command

        with app.app_context():
            dev = db.session.get(Device, device_pk)
            cmd = build_command(dev, ctype, args or {})
            cmd.status = status
            db.session.commit()
            return cmd.id
    return _make