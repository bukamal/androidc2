"""Heartbeat reaper.

A device is considered online only while a socket is actually registered.
The disconnect handler covers the clean path, but a dropped tunnel, a killed
process or a yanked network never delivers a disconnect frame — without this
the UI shows phantom-online devices forever.
"""

from __future__ import annotations

import threading
import time

from c2.agent_ws import connected_devices
from c2.timeutil import utcnow
from c2.ws_dashboard import broadcast
from database import db
from models import Device


def reap_once(app, timeout: int) -> list[str]:
    """Mark timed-out devices offline. Returns the affected device ids."""
    reaped: list[str] = []
    live = connected_devices()

    with app.app_context():
        candidates = Device.query.filter_by(is_online=True).all()
        now = utcnow()
        for dev in candidates:
            if dev.device_id in live:
                continue
            if not dev.is_stale(timeout):
                continue
            dev.is_online = False
            reaped.append(dev.device_id)

        if reaped:
            db.session.commit()
            for device_id in reaped:
                dev = Device.query.filter_by(device_id=device_id).first()
                if dev:
                    payload = dev.to_dict()
                    broadcast("device_update", payload)
                    broadcast("device_update", payload, room="all_devices")
                    broadcast("device_update", payload, room=f"device_{dev.id}")

    return reaped


def start_reaper(app, timeout: int, interval: int) -> threading.Thread:
    def loop():
        while True:
            time.sleep(interval)
            try:
                reaped = reap_once(app, timeout)
                if reaped:
                    print(f"[reaper] marked offline: {', '.join(reaped)}", flush=True)
            except Exception as exc:  # a reaper must never kill the process
                print(f"[reaper] error: {exc}", flush=True)

    thread = threading.Thread(target=loop, name="c2-reaper", daemon=True)
    thread.start()
    return thread