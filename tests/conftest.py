"""Shared fixtures.

Environment is pinned before anything imports ``config`` so the suite never
touches the real database, the real uploads tree, or the real secrets.
"""

import os
import shutil
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="androidc2-tests-")

# Start from a clean slate. The suite pins every variable it depends on, so a
# developer's shell (or a stray .env that got sourced into it) cannot change
# what the tests observe.
for _var in list(os.environ):
    if _var.startswith("C2_"):
        del os.environ[_var]

os.environ["C2_SECRET"] = "test-secret-key"
os.environ["C2_API_KEY"] = "test-agent-key"
os.environ["C2_OPERATOR_NAME"] = "tester"
os.environ["C2_OPERATOR_PASSWORD"] = "test-operator-pass"
os.environ["C2_DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["C2_UPLOAD_FOLDER"] = os.path.join(_TMP, "data", "uploads")
os.environ["C2_LEGACY_UPLOAD_FOLDER"] = os.path.join(_TMP, "no-such-legacy-dir")
os.environ["C2_DB_URI"] = "sqlite:///" + os.path.join(_TMP, "test.db")
os.environ["C2_HOST"] = "127.0.0.1"
# The auth/CI suites assert on 401/403 behaviour, so they must run against
# the strictest mode regardless of how the developer's shell is configured.
os.environ["C2_AUTH"] = "operator"

API_KEY = "test-agent-key"
# Must match C2_OPERATOR_NAME above: create_app() bootstraps this operator,
# and clean_db drops the table between tests.
OPERATOR_NAME = "tester"
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

    # The bootstrap operator is created by create_app() from the pinned env
    # above; make sure it exists even if that path changes.
    from c2 import operators as ops
    with application.app_context():
        if ops.Operator.query.filter_by(name=OPERATOR_NAME).first() is None:
            ops.create_operator(OPERATOR_NAME, OPERATOR_PASSWORD)

    return application


def login(client, name=OPERATOR_NAME, password=OPERATOR_PASSWORD, csrf=True):
    """Log a client in and attach the CSRF token to its default headers."""
    client.post("/login", data={"name": name, "password": password})
    if not csrf:
        return client
    token = client.get("/api/session").get_json()["csrf_token"]
    client.environ_base["HTTP_X_CSRF_TOKEN"] = token
    return client


@pytest.fixture(autouse=True)
def clean_db(app):
    reset_registry()
    auth._FAILURES.clear()
    with app.app_context():
        db.drop_all()
        db.create_all()
        # create_app() bootstraps the operator once at session start; dropping
        # the tables wipes it, so put it back for this test.
        from c2 import operators as ops

        if ops.Operator.query.filter_by(name=OPERATOR_NAME).first() is None:
            ops.create_operator(OPERATOR_NAME, OPERATOR_PASSWORD)
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
    return login(client)


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