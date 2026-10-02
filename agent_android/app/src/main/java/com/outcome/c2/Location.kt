package com.outcome.c2

import android.content.Context
import android.location.LocationManager
import org.json.JSONObject
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

object Location {

    fun get(ctx: Context): JSONObject {
        val lm = ctx.getSystemService(Context.LOCATION_SERVICE) as LocationManager
        val providers = listOf(
            LocationManager.GPS_PROVIDER,
            LocationManager.NETWORK_PROVIDER,
            LocationManager.PASSIVE_PROVIDER
        )

        var best: android.location.Location? = null
        for (p in providers) {
            try {
                val loc = lm.getLastKnownLocation(p) ?: continue
                if (best == null || loc.time > best!!.time) best = loc
            } catch (_: Exception) {}
        }

        val active = providers.firstOrNull {
            runCatching { lm.isProviderEnabled(it) }.getOrDefault(false)
        }
        if (active != null) {
            try {
                val latch = CountDownLatch(1)
                val listener = object : android.location.LocationListener {
                    override fun onLocationChanged(location: android.location.Location) {
                        if (best == null || location.time > best!!.time) best = location
                        latch.countDown()
                    }
                    override fun onProviderEnabled(provider: String) {}
                    override fun onProviderDisabled(provider: String) {}
                    @Deprecated("") override fun onStatusChanged(p: String?, s: Int, e: android.os.Bundle?) {}
                }
                @Suppress("MissingPermission")
                lm.requestLocationUpdates(active, 0L, 0f, listener, android.os.Looper.getMainLooper())
                latch.await(4, TimeUnit.SECONDS)
                lm.removeUpdates(listener)
            } catch (_: Exception) {}
        }

        val loc = best ?: return JSONObject().put("error", "no_fix")
        return JSONObject()
            .put("lat", loc.latitude)
            .put("lng", loc.longitude)
            .put("accuracy", loc.accuracy.toDouble())
            .put("provider", loc.provider)
            .put("timestamp", loc.time)
    }
}
