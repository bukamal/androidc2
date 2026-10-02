package com.outcome.c2

import android.content.Context
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.os.Handler
import android.os.HandlerThread
import android.util.Base64
import android.util.DisplayMetrics
import android.util.Log
import android.view.WindowManager
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/**
 * Long-lived screen capture session. Reuses a single VirtualDisplay
 * across multiple captures — required on Android 14+ where a MediaProjection
 * can only create one VirtualDisplay.
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

    // Latest frame is kept here as soon as it arrives.
    private val latest: AtomicReference<Bitmap?> = AtomicReference(null)

    init {
        val wm = ctx.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val metrics = DisplayMetrics()
        @Suppress("DEPRECATION")
        wm.defaultDisplay.getRealMetrics(metrics)
        width = metrics.widthPixels
        height = metrics.heightPixels
        density = metrics.densityDpi
        Log.i(TAG, "session init ${width}x$height @${density}dpi")

        // Dedicated handler thread — NOT the main looper
        val t = HandlerThread("c2-capture").also { it.start() }
        thread = t
        val h = Handler(t.looper)
        handler = h

        val r = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 3)
        reader = r

        // Continuous listener — captures frames as they arrive
        r.setOnImageAvailableListener({ ir ->
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
     * Capture a fresh frame. Waits for the latest frame to arrive,
     * up to `timeoutMs` milliseconds.
     */
    fun capture(timeoutMs: Long = 6000): JSONObject {
        val r = reader ?: return JSONObject().put("error", "reader_null")
        if (vd == null) return JSONObject().put("error", "virtual_display_null")

        // Clear stale frame
        latest.getAndSet(null)?.recycle()

        // Wait for the listener to deliver at least one frame
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

    fun release() {
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


/**
 * One-shot capture object, kept for API compatibility.
 * Prefer using ScreenCaptureSession for multiple captures.
 */
object Screenshot {
    fun capture(ctx: Context, projection: MediaProjection): JSONObject {
        return try {
            val session = ScreenCaptureSession(ctx, projection)
            // Give the pipeline a moment to deliver a frame
            Thread.sleep(400)
            val result = session.capture()
            session.release()
            result
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "capture_failed")
        }
    }
}
