"""Operator password resolution, shared by the server and the bridge.

The two processes are separate, run by the same user, on the same box. If
they each needed their own copy of the password the operator would have to
keep two values in sync by hand — so they resolve it identically instead.
"""

import asyncio
import importlib
import os
import sys
from pathlib import Path

import pytest


def load_bridge(monkeypatch):
    """Import telegram_bridge fresh with a controlled environment.

    ``config`` is dropped alongside it on purpose: Config snapshots the
    environment at import time, so a cached module would keep whatever the
    suite set at startup and make every resolution path untestable.
    """
    for name in list(sys.modules):
        if name in ("telegram_bridge", "config"):
            del sys.modules[name]

    monkeypatch.setenv("C2_BASE", "http://127.0.0.1:1")
    monkeypatch.setenv("TG_BOT_TOKEN", "000000:test")
    monkeypatch.setenv("TG_CHAT_ID", "1")
    return importlib.import_module("telegram_bridge")


def test_env_var_wins(monkeypatch):
    monkeypatch.setenv("C2_OPERATOR_PASSWORD", "from-env")
    assert load_bridge(monkeypatch).C2_PASSWORD == "from-env"


def test_explicit_file_is_honoured(monkeypatch, tmp_path):
    secret = tmp_path / "custom.secret"
    secret.write_text("file-secret\n", encoding="utf-8")

    monkeypatch.delenv("C2_OPERATOR_PASSWORD", raising=False)
    monkeypatch.setenv("C2_OPERATOR_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("C2_DATA_DIR", str(tmp_path / "does-not-exist"))

    assert load_bridge(monkeypatch).C2_PASSWORD == "file-secret"


def test_falls_back_to_the_generated_data_dir(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "c2_operator_password.secret").write_text(
        "generated-secret\n", encoding="utf-8"
    )

    monkeypatch.delenv("C2_OPERATOR_PASSWORD", raising=False)
    monkeypatch.delenv("C2_OPERATOR_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("C2_DATA_DIR", str(data_dir))
    # config.py would otherwise regenerate into the real repo data dir.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    assert load_bridge(monkeypatch).C2_PASSWORD == "generated-secret"


def test_empty_secret_file_does_not_win(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "c2_operator_password.secret").write_text("   \n", encoding="utf-8")

    monkeypatch.delenv("C2_OPERATOR_PASSWORD", raising=False)
    monkeypatch.delenv("C2_OPERATOR_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("C2_DATA_DIR", str(data_dir))

    # Whitespace-only is not a password; the bridge must end up empty rather
    # than logging in with a blank string.
    assert load_bridge(monkeypatch).C2_PASSWORD == ""


def test_bridge_reads_exactly_what_the_server_generated(monkeypatch, tmp_path):
    """Generate with the server, then read it back with the bridge."""
    data_dir = tmp_path / "shared"
    monkeypatch.delenv("C2_OPERATOR_PASSWORD", raising=False)
    monkeypatch.delenv("C2_OPERATOR_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("C2_DATA_DIR", str(data_dir))

    server_password = None
    for name in list(sys.modules):
        if name == "config":
            del sys.modules[name]
    import config

    server_password = config.Config.OPERATOR_PASSWORD
    assert server_password

    bridge = load_bridge(monkeypatch)
    assert bridge.C2_PASSWORD == server_password


def test_bridge_never_generates_a_password(monkeypatch, tmp_path):
    """If the server has not run, the bridge must stay empty and not create
    a secret file the server would then disagree with."""
    data_dir = tmp_path / "cold"
    monkeypatch.delenv("C2_OPERATOR_PASSWORD", raising=False)
    monkeypatch.delenv("C2_OPERATOR_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("C2_DATA_DIR", str(data_dir))

    bridge = load_bridge(monkeypatch)

    assert bridge.C2_PASSWORD == ""
    assert not (data_dir / "c2_operator_password.secret").exists()


def test_missing_password_short_circuits_before_any_request(monkeypatch, tmp_path):
    monkeypatch.delenv("C2_OPERATOR_PASSWORD", raising=False)
    monkeypatch.delenv("C2_OPERATOR_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("C2_DATA_DIR", str(tmp_path / "empty"))

    bridge = load_bridge(monkeypatch)
    assert bridge.C2_PASSWORD == ""

    # A blank password must never be sent: fail before touching the network.
    def explode(*_a, **_kw):
        raise AssertionError("c2_login attempted a request with no password")

    monkeypatch.setattr(bridge.client, "post", explode)
    bridge._c2_session_ok = False

    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(bridge.c2_login()) is False
    finally:
        loop.close()