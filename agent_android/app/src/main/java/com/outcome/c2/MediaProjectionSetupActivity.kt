package com.outcome.c2

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.media.projection.MediaProjectionManager
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
        }
        finish()
    }
}
