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
        val mpm = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        startActivityForResult(mpm.createScreenCaptureIntent(), REQ)
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (requestCode == REQ && resultCode == RESULT_OK && data != null) {
            getSharedPreferences("c2", MODE_PRIVATE).edit()
                .putInt("proj_code", resultCode)
                .putString("proj_data", data.toUri(0))
                .apply()

            // Restart C2Service so it picks up the new projection permission
            val svc = Intent(this, C2Service::class.java)
            stopService(svc)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                startForegroundService(svc)
            else
                startService(svc)
        }
        finish()
    }
}
