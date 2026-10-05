package com.outcome.c2

import android.content.Context
import android.media.projection.MediaProjection
import android.util.Log

/**
 * Single source of truth for "is a screen projection live right now?".
 *
 * The problem this solves: consent for MediaProjection cannot be persisted.
 * Since Android 10 the token is single-use, so once a capture finishes the
 * projection stops — but any flag left in SharedPreferences keeps claiming
 * otherwise, and code that trusts it re-prompts forever.
 *
 * So: no stored flag decides this. The live object decides it, and the flag
 * is cleared the moment the projection stops.
 */
object ProjectionState {

    private const val TAG = "ProjectionState"

    @Volatile
    private var live: MediaProjection? = null

    /** True only while a projection is genuinely usable. */
    fun isActive(): Boolean = live != null

    fun current(): MediaProjection? = live

    /**
     * Publish the projection and clear the stale consent flag, so a crash or
     * a later app open cannot mistake this for "still consented".
     */
    fun attach(projection: MediaProjection?, prefs: Context? = null) {
        live = projection
        clearStoredConsent(prefs)
        Log.i(TAG, "attached: active=${projection != null}")
    }

    fun detach(prefs: Context? = null) {
        live = null
        clearStoredConsent(prefs)
        Log.i(TAG, "detached")
    }

    private fun clearStoredConsent(prefs: Context?) {
        try {
            (prefs ?: return).getSharedPreferences("c2", Context.MODE_PRIVATE)
                .edit()
                .remove("proj_code")
                .remove("proj_data")
                .apply()
        } catch (_: Exception) {
        }
    }

    /** Test seam: reset without touching a real projection. */
    fun reset() {
        live = null
    }
}