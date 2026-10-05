"""Structured logging, request-id correlation, and secret redaction.

The redaction tests matter more than the formatting ones: a logging helper is
exactly the kind of change that quietly starts writing credentials to disk.
"""

import io
import json
import logging

import pytest

from c2 import logs


@pytest.fixture
def text_log():
    buf = io.StringIO()
    logger = logs.configure(level="debug", fmt="text")
    logger.handlers[0].stream = buf
    return buf


@pytest.fixture
def json_log():
    buf = io.StringIO()
    logger = logs.configure(level="debug", fmt="json")
    logger.handlers[0].stream = buf
    return buf


@pytest.fixture(autouse=True)
def _clean_context():
    logs.set_request_id(None)
    logs.unbind_all()
    logs.register_secrets([])
    yield
    logs.set_request_id(None)
    logs.unbind_all()


def emit(logger, event, **fields):
    logger.info(event, extra={"structured": fields})


# ------------------------------------------------------------- formatting

def test_text_line_is_greppable(text_log):
    emit(logs.get_logger("c2.agent"), "agent.accepted", device="abc123")
    line = text_log.getvalue()

    assert "agent.accepted" in line
    assert "device=abc123" in line
    assert "INFO" in line
    assert "c2.agent" in line


def test_every_line_starts_with_a_local_timestamp(text_log):
    emit(logs.get_logger("c2.app"), "x")
    stamp = text_log.getvalue().split(" ")[0]
    assert len(stamp) == 12, "expected HH:MM:SS.mmm"
    assert stamp.count(":") == 2


def test_json_format_is_one_object_per_line(json_log):
    log = logs.get_logger("c2.agent")
    emit(log, "one", a=1)
    emit(log, "two", b=2)

    lines = [l for l in json_log.getvalue().strip().split("\n") if l]
    assert len(lines) == 2
    for line in lines:
        obj = json.loads(line)
        assert {"ts", "level", "logger", "event"} <= set(obj)


def test_values_with_spaces_are_quoted(text_log):
    emit(logs.get_logger("c2.x"), "e", note="two words")
    assert 'note="two words"' in text_log.getvalue()


def test_none_fields_are_omitted(text_log):
    """A field explicitly set to None is noise; drop it rather than log `-`."""
    emit(logs.get_logger("c2.x"), "e", nothing=None, something=1)
    out = text_log.getvalue()
    assert "nothing" not in out
    assert "something=1" in out


def test_booleans_render_lowercase(text_log):
    emit(logs.get_logger("c2.x"), "e", ok=True, bad=False)
    out = text_log.getvalue()
    assert "ok=true" in out
    assert "bad=false" in out


def test_level_filtering_drops_debug_when_info():
    buf = io.StringIO()
    logger = logs.configure(level="info", fmt="text")
    logger.handlers[0].stream = buf
    log = logs.get_logger("c2.x")

    log.debug("hidden")
    log.info("shown")

    out = buf.getvalue()
    assert "hidden" not in out
    assert "shown" in out


# ---------------------------------------------------------- request id

def test_request_id_appears_in_every_line_while_bound(text_log):
    logs.set_request_id("deadbeefcafe")
    emit(logs.get_logger("c2.x"), "one")
    emit(logs.get_logger("c2.x"), "two")

    out = text_log.getvalue()
    assert out.count("rid=deadbeefcafe") == 2


def test_request_id_is_absent_when_unset(text_log):
    emit(logs.get_logger("c2.x"), "e")
    assert "rid=" not in text_log.getvalue()


def test_request_id_is_json_field(json_log):
    logs.set_request_id("abc123")
    emit(logs.get_logger("c2.x"), "e")
    assert json.loads(json_log.getvalue())["rid"] == "abc123"


def test_new_request_ids_are_unique():
    ids = {logs.new_request_id() for _ in range(500)}
    assert len(ids) == 500
    assert all(len(i) == 16 for i in ids)


# ----------------------------------------------------------- bind fields

def test_bound_fields_appear_on_every_line(text_log):
    logs.bind(device="dev-1", actor="operator")
    emit(logs.get_logger("c2.x"), "one")
    emit(logs.get_logger("c2.x"), "two")

    out = text_log.getvalue()
    assert out.count("device=dev-1") == 2
    assert out.count("actor=operator") == 2


def test_unbind_clears_them(text_log):
    logs.bind(device="dev-1")
    logs.unbind_all()
    emit(logs.get_logger("c2.x"), "e")
    assert "device=" not in text_log.getvalue()


def test_per_line_fields_override_bound(text_log):
    logs.bind(device="bound")
    emit(logs.get_logger("c2.x"), "e", device="explicit")
    assert "device=explicit" in text_log.getvalue()
    assert "device=bound" not in text_log.getvalue()


# -------------------------------------------------------------- redaction

@pytest.mark.parametrize("name", [
    "password", "api_key", "apikey", "apiKey", "secret", "token",
    "authorization", "cookie", "credential", "operator_password",
    "TG_BOT_TOKEN", "x-api-key",
])
def test_sensitive_field_names_are_masked(text_log, name):
    emit(logs.get_logger("c2.x"), "e", **{name: "hunter2"})
    assert "hunter2" not in text_log.getvalue()
    assert logs.REDACTED in text_log.getvalue()


def test_registered_secret_is_masked_under_any_field_name(text_log):
    """A secret passed as `value` must still be caught by value."""
    logs.register_secrets(["supersecretvalue"])
    emit(logs.get_logger("c2.x"), "e", value="prefix-supersecretvalue-suffix")

    out = text_log.getvalue()
    assert "supersecretvalue" not in out
    assert "prefix-" in out and "-suffix" in out


def test_registered_secret_is_masked_in_json(json_log):
    logs.register_secrets(["supersecretvalue"])
    emit(logs.get_logger("c2.x"), "e", note="has supersecretvalue inside")

    assert "supersecretvalue" not in json_log.getvalue()


def test_very_short_values_are_not_treated_as_secrets(text_log):
    """Registering a 3-char string must not redact half the log."""
    logs.register_secrets(["abc"])
    emit(logs.get_logger("c2.x"), "e", note="abcdefghij")
    assert "abcdefghij" in text_log.getvalue()


def test_empty_secrets_are_ignored():
    logs.register_secrets(["", None, 12345])
    assert "12345" not in logs.secret_snapshot()


def test_secret_snapshot_is_a_copy():
    logs.register_secrets(["onething"])
    snapshot = logs.secret_snapshot()
    snapshot.add("forged")
    assert "forged" not in logs.secret_snapshot()


def test_redact_value_leaves_ordinary_fields_alone():
    assert logs.redact_value("model", "Pixel 8") == "Pixel 8"
    assert logs.redact_value("count", 3) == 3


# --------------------------------------------------------- app integration

def test_request_id_is_echoed_on_responses(client):
    r = client.get("/login")
    assert logs.REQUEST_ID_HEADER in r.headers
    assert len(r.headers[logs.REQUEST_ID_HEADER]) == 16


def test_client_supplied_request_id_is_ignored_by_default(client):
    r = client.get("/login", headers={logs.REQUEST_ID_HEADER: "spoofed-id"})
    assert r.headers[logs.REQUEST_ID_HEADER] != "spoofed-id"


def test_client_supplied_request_id_is_honoured_when_trusted(app):
    app.config["TRUST_PROXY_REQUEST_ID"] = True
    with app.app_context():
        from config import Config
        original = Config.TRUST_PROXY_REQUEST_ID
        Config.TRUST_PROXY_REQUEST_ID = True
        try:
            r = app.test_client().get(
                "/login", headers={logs.REQUEST_ID_HEADER: "fromproxy123"})
            assert r.headers[logs.REQUEST_ID_HEADER] == "fromproxy123"
        finally:
            Config.TRUST_PROXY_REQUEST_ID = original


def test_malformed_request_id_is_replaced(app):
    app.config["TRUST_PROXY_REQUEST_ID"] = True
    from config import Config
    original = Config.TRUST_PROXY_REQUEST_ID
    Config.TRUST_PROXY_REQUEST_ID = True
    try:
        for bad in ("has space", "x" * 200, "semi;colon", ""):
            r = app.test_client().get(
                "/login", headers={logs.REQUEST_ID_HEADER: bad})
            assert r.headers[logs.REQUEST_ID_HEADER] not in ("", bad[:16]) \
                or bad == ""
    finally:
        Config.TRUST_PROXY_REQUEST_ID = original


def test_request_id_does_not_leak_between_requests(client):
    """Teardown must clear the contextvar, or a reused worker thread would
    stamp the previous request's id onto the next one."""
    first = client.get("/login").headers[logs.REQUEST_ID_HEADER]
    second = client.get("/login").headers[logs.REQUEST_ID_HEADER]
    assert first != second


def _capture_startup_banner():
    """Grab the c2 logger's stream directly: the handler holds a reference to
    the stream it was created with, so capsys would not see it."""
    import app as app_module

    buf = io.StringIO()
    logs.configure().handlers[0].stream = buf
    app_module._startup_banner()
    return buf.getvalue()


def test_startup_banner_never_prints_the_password():
    from config import Config

    out = _capture_startup_banner()

    assert "server.starting" in out
    assert Config.OPERATOR_PASSWORD not in out
    assert Config.API_KEY not in out
    assert Config.SECRET_KEY not in out


def test_startup_banner_includes_the_key_fingerprint():
    from c2.auth import key_fingerprint
    from config import Config

    out = _capture_startup_banner()
    assert key_fingerprint(Config.API_KEY) in out
    assert f"api_key_len={len(Config.API_KEY)}" in out


def test_werkzeug_is_silenced():
    """Otherwise the WebSocket teardown 500s drown everything else out."""
    logs.configure()
    werkzeug = logging.getLogger("werkzeug")
    assert werkzeug.level >= logging.WARNING
    assert werkzeug.propagate is False


def test_server_still_serves_requests_after_logging_setup(client):
    from conftest import OPERATOR_NAME, OPERATOR_PASSWORD

    client.post("/login", data={"name": OPERATOR_NAME,
                                "password": OPERATOR_PASSWORD})
    assert client.get("/api/devices").status_code == 200

# ------------------------------------------------- metadata vs. credential

@pytest.mark.parametrize("name", [
    "api_key_fp", "api_key_len", "secret_len", "password_count",
    "token_id", "credential_digest", "cookie_at",
])
def test_derived_metadata_survives_the_name_rule(text_log, name):
    """`api_key_fp` is a fingerprint, not a key. Redacting it would hide the
    one value that makes a key mismatch diagnosable."""
    emit(logs.get_logger("c2.x"), "e", **{name: "abc123"})
    assert "abc123" in text_log.getvalue()
    assert logs.REDACTED not in text_log.getvalue()


def test_metadata_fields_are_still_value_redacted(text_log):
    """The suffix exemption only waives the name check, never the value check."""
    logs.register_secrets(["supersecretvalue"])
    emit(logs.get_logger("c2.x"), "e", api_key_fp="leaks supersecretvalue here")

    assert "supersecretvalue" not in text_log.getvalue()


def test_bare_credential_names_are_still_masked(text_log):
    emit(logs.get_logger("c2.x"), "e", api_key="s3cr3t-value")
    assert "s3cr3t-value" not in text_log.getvalue()


def test_redact_value_is_case_insensitive():
    assert logs.redact_value("API_KEY", "x") == logs.REDACTED
    assert logs.redact_value("PassWord", "x") == logs.REDACTED
    assert logs.redact_value("api_key_FP", "x") == "x"
