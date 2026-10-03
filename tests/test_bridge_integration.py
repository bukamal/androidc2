"""End-to-end check: the Telegram bridge against a live server.

The bridge talks to the *operator* API, which is session-authenticated. This
test boots a real server, points the bridge module at it and exercises the
login -> CSRF -> command-queue path so a change to either side cannot silently
break the other.
"""

import asyncio
import importlib
import os
import socket
import sys
import threading
import time

import pytest

from conftest import OPERATOR_PASSWORD


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server(request):
    from app import create_app
    from c2.ws_dashboard import socketio

    port = _free_port()
    app = create_app(start_background=False)

    thread = threading.Thread(
        target=lambda: socketio.run(
            app, host="127.0.0.1", port=port,
            allow_unsafe_werkzeug=True, log_output=False,
        ),
        daemon=True,
    )
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:
        pytest.skip("could not bind live server")

    yield base


@pytest.fixture(scope="module")
def bridge(live_server, tmp_path_factory):
    home = tmp_path_factory.mktemp("bridge-home")
    os.environ["HOME"] = str(home)
    os.environ["TG_BOT_TOKEN"] = "000000:test"
    os.environ["TG_CHAT_ID"] = "1"
    os.environ["C2_BASE"] = live_server
    os.environ["C2_OPERATOR_PASSWORD"] = OPERATOR_PASSWORD
    os.environ["C2_API_KEY"] = "test-agent-key"

    sys.modules.pop("telegram_bridge", None)
    module = importlib.import_module("telegram_bridge")

    yield module

    try:
        asyncio.run(module.client.aclose())
    except Exception:
        pass
    sys.modules.pop("telegram_bridge", None)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_bridge_logs_in_and_reaches_the_operator_api(bridge, live_server):
    assert run(bridge.c2_login()) is True
    assert bridge._c2_csrf


def test_bridge_sees_an_empty_device_list(bridge):
    assert run(bridge.c2_devices()) == []


def test_bridge_queues_a_command_through_the_protected_route(bridge, make_device):
    dev_pk = make_device("bridge-dev")

    result = run(bridge.c2_send_command(dev_pk, "device_info", {}))

    assert "error" not in result, result
    assert result["status"] in ("pending", "sent")
    assert isinstance(result["id"], int)


def test_bridge_recovers_a_dropped_session_on_its_own(bridge, make_device):
    """A dead session is re-established instead of erroring out."""
    dev_pk = make_device("bridge-recover")
    bridge._c2_session_ok = False
    bridge._c2_csrf = ""

    result = run(bridge.c2_send_command(dev_pk, "device_info", {}))
    assert "error" not in result, result
    assert bridge._c2_session_ok is True
    assert bridge._c2_csrf


def test_bridge_without_a_password_reports_no_session(bridge, make_device):
    dev_pk = make_device("bridge-nopass")
    original = bridge.C2_PASSWORD
    bridge.C2_PASSWORD = ""
    bridge._c2_session_ok = False
    bridge._c2_csrf = ""
    try:
        assert run(bridge.c2_send_command(dev_pk, "device_info", {})) == \
            {"error": "no_session"}
        assert run(bridge.c2_devices()) == []
        assert run(bridge.c2_files(dev_pk)) == []
        assert run(bridge.c2_file_bytes(1)) is None
        assert run(bridge.c2_device(dev_pk)) is None
        assert run(bridge.c2_command_result(dev_pk, 1)) is None
    finally:
        bridge.C2_PASSWORD = original
        bridge._c2_session_ok = False


# ------------------------------------------------------------- md() escaping

@pytest.mark.parametrize("raw", [
    "Pixel_8_Pro",
    "Galaxy *S22*",
    "SM-G991B [test]",
    "1.2.3 (beta)",
    "a-b_c",
    "back\\slash",
    "`code`",
])
def test_md_neutralises_markdown_metacharacters(raw):
    out = bridge_md(raw)
    for ch in "_*[]()~`>#+-=|{}.!":
        assert f"\\{ch}" in out or ch not in out


def test_md_renders_back_to_the_original(bridge):
    assert bridge_md("Pixel_8") == r"Pixel\_8"
    assert bridge_md(None) == ""
    assert bridge_md(42) == "42"


def bridge_md(value):
    sys.modules.pop("telegram_bridge", None)
    os.environ.setdefault("TG_BOT_TOKEN", "000000:test")
    os.environ.setdefault("TG_CHAT_ID", "1")
    module = importlib.import_module("telegram_bridge")
    return module.md(value)