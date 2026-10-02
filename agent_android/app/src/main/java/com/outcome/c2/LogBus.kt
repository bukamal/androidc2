package com.outcome.c2

import android.content.Context
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Tiny in-app log buffer. Stores recent lines in SharedPreferences
 * so both MainActivity and C2Service can read/write them.
 */
object LogBus {
    private const val PREFS = "c2_logs"
    private const val KEY = "lines"
    private const val MAX_LINES = 400
    private val fmt = SimpleDateFormat("HH:mm:ss", Locale.US)

    @Synchronized
    fun append(ctx: Context, tag: String, msg: String) {
        val prefs = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val existing = prefs.getString(KEY, "") ?: ""
        val stamp = fmt.format(Date())
        val line = "$stamp [$tag] $msg"
        val combined = if (existing.isEmpty()) line else "$existing\n$line"
        val trimmed = if (combined.lines().size > MAX_LINES) {
            combined.lines().takeLast(MAX_LINES).joinToString("\n")
        } else combined
        prefs.edit().putString(KEY, trimmed).apply()
    }

    fun read(ctx: Context): String {
        val prefs = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        return prefs.getString(KEY, "") ?: ""
    }

    fun clear(ctx: Context) {
        val prefs = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        prefs.edit().clear().apply()
    }
}
