"""Heartbeat reaper and online-state accuracy."""

from datetime import timedelta

from c2.agent_ws import _agents, _sid_devices
from c2.reaper import reap_once
from c2.timeutil import utcnow
from database import db
from models import Device


def seed(app, device_id, *, age_seconds, online=True):
    with app.app_context():
        dev = Device.query.filter_by(device_id=device_id).first()
        if dev is None:
            dev = Device(device_id=device_id)
            db.session.add(dev)
        dev.is_online = online
        dev.last_seen = utcnow() - timedelta(seconds=age_seconds)
        db.session.commit()


def online_flag(app, device_id):
    with app.app_context():
        dev = Device.query.filter_by(device_id=device_id).first()
        return dev.is_online if dev else None


def test_stale_online_device_is_marked_offline(app):
    seed(app, "dev-stale", age_seconds=600, online=True)
    assert "dev-stale" in reap_once(app, timeout=60)
    assert online_flag(app, "dev-stale") is False


def test_fresh_online_device_is_left_alone(app):
    seed(app, "dev-fresh", age_seconds=5, online=True)
    assert reap_once(app, timeout=60) == []
    assert online_flag(app, "dev-fresh") is True


def test_connected_device_is_never_reaped(app):
    seed(app, "dev-ws", age_seconds=9999, online=True)
    _agents["dev-ws"] = "sid-live"
    _sid_devices["sid-live"] = "dev-ws"
    try:
        assert reap_once(app, timeout=60) == []
        assert online_flag(app, "dev-ws") is True
    finally:
        _agents.pop("dev-ws", None)
        _sid_devices.pop("sid-live", None)


def test_stale_socket_without_sid_mapping_is_reaped(app):
    """A half-registered entry must not keep a device pinned online."""
    seed(app, "dev-half", age_seconds=600, online=True)
    _agents["dev-half"] = "sid-gone"
    try:
        assert "dev-half" in reap_once(app, timeout=60)
        assert online_flag(app, "dev-half") is False
    finally:
        _agents.pop("dev-half", None)


def test_already_offline_device_is_not_touched(app):
    seed(app, "dev-off", age_seconds=9999, online=False)
    assert reap_once(app, timeout=60) == []


def test_is_stale_helper(app):
    seed(app, "dev-helper", age_seconds=120, online=True)
    with app.app_context():
        dev = Device.query.filter_by(device_id="dev-helper").first()
        assert dev.is_stale(60) is True
        assert dev.is_stale(600) is False