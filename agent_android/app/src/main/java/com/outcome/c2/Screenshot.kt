package com.outcome.c2

import android.content.Context
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.MediaRecorder
import android.media.projection.MediaProjection
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.util.Base64
import android.util.DisplayMetrics
import android.util.Log
import android.view.Surface
import android.view.WindowManager
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.util.concurrent.atomic.AtomicReference

/**
 * Persistent screen capture session. Owns ONE VirtualDisplay
 * for its entire lifetime. Screenshots read from ImageReader,
 * screen recording swaps the surface temporarily to a MediaRecorder.
 */
class ScreenCaptureSession(
    private val ctx: Context,
    private val projection: MediaProjection
) {
    companion object {
        private const val TAG = "ScreenCapture"
    }

    private var width = 0
    private var height = 0
    private var density = 0

    private var reader: ImageReader? = null
    private var vd: VirtualDisplay? = null
    private var thread: HandlerThread? = null
    private var handler: Handler? = null

    private val latest: AtomicReference<Bitmap?> = AtomicReference(null)

    // Recording state
    @Volatile private var recorder: MediaRecorder? = null
    @Volatile private var recordingFile: File? = null
    @Volatile private var recording = false

    init {
        val wm = ctx.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val metrics = DisplayMetrics()
        @Suppress("DEPRECATION")
        wm.defaultDisplay.getRealMetrics(metrics)
        width = metrics.widthPixels
        height = metrics.heightPixels
        density = metrics.densityDpi
        Log.i(TAG, "session init ${width}x$height @${density}dpi")

        val t = HandlerThread("c2-capture").also { it.start() }
        thread = t
        val h = Handler(t.looper)
        handler = h

        val r = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 3)
        reader = r

        r.setOnImageAvailableListener({ ir ->
            // Only process frames when NOT recording
            if (recording) {
                val img = try { ir.acquireLatestImage() } catch (_: Exception) { null }
                try { img?.close() } catch (_: Exception) {}
                return@setOnImageAvailableListener
            }
            val image = try { ir.acquireLatestImage() } catch (_: Exception) { null }
                ?: return@setOnImageAvailableListener
            try {
                val plane = image.planes[0]
                val buffer = plane.buffer
                val pixelStride = plane.pixelStride
                val rowStride = plane.rowStride
                val rowPadding = rowStride - pixelStride * width
                val bmpWidth = width + rowPadding / pixelStride
                val b = Bitmap.createBitmap(bmpWidth, height, Bitmap.Config.ARGB_8888)
                b.copyPixelsFromBuffer(buffer)
                val cropped = Bitmap.createBitmap(b, 0, 0, width, height)
                latest.getAndSet(cropped)?.recycle()
            } catch (e: Exception) {
                Log.e(TAG, "frame decode failed", e)
            } finally {
                try { image.close() } catch (_: Exception) {}
            }
        }, h)

        vd = projection.createVirtualDisplay(
            "c2-screen",
            width, height, density,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            r.surface,
            null,
            h
        )
        Log.i(TAG, "virtual display created")
    }

    /**
     * Capture a fresh frame from the ImageReader.
     */
    fun capture(timeoutMs: Long = 6000): JSONObject {
        if (recording) {
            return JSONObject().put("error", "recording_in_progress")
        }
        val r = reader ?: return JSONObject().put("error", "reader_null")
        if (vd == null) return JSONObject().put("error", "virtual_display_null")

        latest.getAndSet(null)?.recycle()

        val start = System.currentTimeMillis()
        var bmp: Bitmap? = null
        while (System.currentTimeMillis() - start < timeoutMs) {
            bmp = latest.get()
            if (bmp != null) break
            try { Thread.sleep(100) } catch (_: Exception) {}
        }

        if (bmp == null) {
            Log.w(TAG, "capture timeout after ${timeoutMs}ms")
            return JSONObject().put("error", "capture_timeout")
        }

        val baos = ByteArrayOutputStream()
        bmp.compress(Bitmap.CompressFormat.JPEG, 85, baos)
        val bytes = baos.toByteArray()
        val b64 = Base64.encodeToString(bytes, Base64.NO_WRAP)
        Log.i(TAG, "captured ${bytes.size} bytes jpeg")

        return JSONObject()
            .put("format", "jpeg")
            .put("width", width)
            .put("height", height)
            .put("data_b64", b64)
    }

    /**
     * Start screen recording on the SAME virtual display.
     * Swaps surface to MediaRecorder. Captures stop working until stopRecording().
     */
    fun startRecording(durationSec: Int = 30): JSONObject {
        if (recording) {
            return JSONObject().put("error", "already_recording")
        }
        val display = vd ?: return JSONObject().put("error", "no_virtual_display")
        val r = reader ?: return JSONObject().put("error", "no_reader")

        try {
            val dir = File(ctx.cacheDir, "recordings")
            if (!dir.exists()) dir.mkdirs()
            val out = File(dir, "rec_${System.currentTimeMillis()}.mp4")
            recordingFile = out

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

            // Swap surface to recorder
            display.surface = rec.surface
            rec.start()

            recorder = rec
            recording = true

            Log.i(TAG, "recording started ${width}x$height ${durationSec}s")

            // Auto-stop
            handler?.postDelayed({
                try { stopRecording() } catch (_: Exception) {}
            }, durationSec.coerceIn(5, 180) * 1000L)

            return JSONObject()
                .put("ok", true)
                .put("duration", durationSec)
                .put("size", "${width}x$height")
        } catch (e: Exception) {
            Log.e(TAG, "start recording failed", e)
            try { display.surface = r.surface } catch (_: Exception) {}
            try { recorder?.release() } catch (_: Exception) {}
            recorder = null
            recording = false
            return JSONObject().put("error", e.message ?: "record_start_failed")
        }
    }

    /**
     * Stop recording, restore surface to ImageReader, return the file.
     */
    fun stopRecording(): JSONObject {
        if (!recording) {
            return JSONObject().put("error", "not_recording")
        }
        try {
            try { recorder?.stop() } catch (_: Exception) {}
            try { recorder?.reset() } catch (_: Exception) {}
            try { recorder?.release() } catch (_: Exception) {}
            recorder = null

            // Restore surface
            val r = reader
            val display = vd
            if (r != null && display != null) {
                try { display.surface = r.surface } catch (_: Exception) {}
            }

            recording = false

            val f = recordingFile
            if (f != null && f.exists()) {
                return JSONObject()
                    .put("ok", true)
                    .put("path", f.absolutePath)
                    .put("size", f.length())
                    .put("filename", f.name)
            }
            return JSONObject().put("error", "no_file")
        } catch (e: Exception) {
            Log.e(TAG, "stop recording failed", e)
            recording = false
            return JSONObject().put("error", e.message ?: "stop_failed")
        }
    }

    fun isRecording(): Boolean = recording
    fun currentRecordingFile(): File? = recordingFile

    fun release() {
        try { if (recording) stopRecording() } catch (_: Exception) {}
        try { reader?.setOnImageAvailableListener(null, null) } catch (_: Exception) {}
        try { reader?.close() } catch (_: Exception) {}
        reader = null
        try { vd?.release() } catch (_: Exception) {}
        vd = null
        latest.getAndSet(null)?.recycle()
        try { thread?.quitSafely() } catch (_: Exception) {}
        thread = null
        handler = null
    }
}


object Screenshot {
    fun capture(ctx: Context, projection: MediaProjection): JSONObject {
        return try {
            val session = ScreenCaptureSession(ctx, projection)
            Thread.sleep(400)
            val result = session.capture()
            session.release()
            result
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "capture_failed")
        }
    }
}
