package com.outcome.c2

import android.content.Context
import android.os.Build
import org.json.JSONObject
import java.util.UUID

object DeviceInfo {

    fun id(ctx: Context): String {
        val prefs = ctx.getSharedPreferences("c2", Context.MODE_PRIVATE)
        var id = prefs.getString("device_id", null)
        if (id == null) {
            id = UUID.randomUUID().toString()
            prefs.edit().putString("device_id", id).apply()
        }
        return id
    }

    fun snapshot(ctx: Context, deviceId: String): JSONObject {
        val bm = ctx.getSystemService(Context.BATTERY_SERVICE) as android.os.BatteryManager
        val battery = bm.getIntProperty(android.os.BatteryManager.BATTERY_PROPERTY_CAPACITY)
        return JSONObject().apply {
            put("device_id", deviceId)
            put("model", Build.MODEL)
            put("manufacturer", Build.MANUFACTURER)
            put("android_version", Build.VERSION.RELEASE)
            put("sdk_int", Build.VERSION.SDK_INT)
            put("hostname", Build.HOST)
            put("battery", battery)
            put("is_admin", false)
        }
    }
}
