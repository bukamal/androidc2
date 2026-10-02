package com.outcome.c2

import android.content.Context
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import org.json.JSONArray
import org.json.JSONObject

object Apps {

    fun list(ctx: Context, systemOnly: Boolean): JSONObject {
        val pm = ctx.packageManager
        val arr = JSONArray()

        val packages = if (android.os.Build.VERSION.SDK_INT >= 33)
            pm.getInstalledPackages(PackageManager.PackageInfoFlags.of(PackageManager.GET_META_DATA.toLong()))
        else
            @Suppress("DEPRECATION") pm.getInstalledPackages(PackageManager.GET_META_DATA)

        for (p in packages) {
            val isSystem = (p.applicationInfo.flags and ApplicationInfo.FLAG_SYSTEM) != 0
            if (systemOnly && !isSystem) continue
            arr.put(JSONObject().apply {
                put("package", p.packageName)
                put("label", p.applicationInfo.loadLabel(pm).toString())
                put("version_name", p.versionName ?: "")
                put("version_code", p.longVersionCode)
                put("is_system", isSystem)
                put("uid", p.applicationInfo.uid)
            })
        }
        return JSONObject().put("count", arr.length()).put("apps", arr)
    }
}
