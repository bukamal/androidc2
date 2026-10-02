package com.outcome.c2

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
            val i = Intent(ctx, C2Service::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                ctx.startForegroundService(i)
            else
                ctx.startService(i)
        }
    }
}
