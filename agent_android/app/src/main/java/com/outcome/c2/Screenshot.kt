package com.outcome.c2

import android.content.Context
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.os.Handler
import android.os.Looper
import android.util.Base64
import android.util.DisplayMetrics
import android.view.WindowManager
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Manages a single long-lived VirtualDisplay attached to a MediaProjection.
 * Subsequent captures reuse the same virtual display — required on Android 14+.
 */
class ScreenCaptureSession(
    private val ctx: Context,
    private val projection: MediaProjection
) {
    private var width = 0
    private var height = 0
    private var density = 0

    private var reader: ImageReader? = null
    private var vd: VirtualDisplay? = null

    private val mainHandler = Handler(Looper.getMainLooper())

    init {
        val wm = ctx.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val metrics = DisplayMetrics()
        @Suppress("DEPRECATION")
        wm.defaultDisplay.getRealMetrics(metrics)
        width = metrics.widthPixels
        height = metrics.heightPixels
        density = metrics.densityDpi

        val r = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 2)
        reader = r

        vd = projection.createVirtualDisplay(
            "c2-screen",
            width, height, density,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            r.surface,
            null,
            mainHandler
        )
    }

    /**
     * Capture a single frame. Returns a JPEG data URI.
     * Never creates a new virtual display — only reads from the existing one.
     */
    fun capture(): JSONObject {
        val r = reader ?: return JSONObject().put("error", "reader_null")
        if (vd == null) return JSONObject().put("error", "virtual_display_null")

        val latch = CountDownLatch(1)
        var bmp: Bitmap? = null

        val listener = ImageReader.OnImageAvailableListener { ir ->
            val image = try { ir.acquireLatestImage() } catch (_: Exception) { null }
                ?: return@OnImageAvailableListener
            try {
                val plane = image.planes[0]
                val buffer = plane.buffer
                val pixelStride = plane.pixelStride
                val rowStride = plane.rowStride
                val rowPadding = rowStride - pixelStride * width
                val b = Bitmap.createBitmap(
                    width + rowPadding / pixelStride,
                    height, Bitmap.Config.ARGB_8888
                )
                b.copyPixelsFromBuffer(buffer)
                bmp = Bitmap.createBitmap(b, 0, 0, width, height)
            } catch (_: Exception) {
            } finally {
                try { image.close() } catch (_: Exception) {}
                latch.countDown()
            }
        }

        r.setOnImageAvailableListener(listener, mainHandler)
        val ok = latch.await(4, TimeUnit.SECONDS)
        r.setOnImageAvailableListener(null, null)

        if (!ok || bmp == null) {
            return JSONObject().put("error", "capture_timeout")
        }

        val baos = ByteArrayOutputStream()
        bmp!!.compress(Bitmap.CompressFormat.JPEG, 85, baos)
        val b64 = Base64.encodeToString(baos.toByteArray(), Base64.NO_WRAP)
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
    }
}


object Screenshot {
    /**
     * Kept for API compatibility. Prefer using ScreenCaptureSession directly.
     * NOTE: MediaProjection#createVirtualDisplay can only be called once on
     * Android 14+, so this method is retained but should not be called twice
     * with the same projection.
     */
    fun capture(ctx: Context, projection: MediaProjection): JSONObject {
        return try {
            val session = ScreenCaptureSession(ctx, projection)
            val result = session.capture()
            session.release()
            result
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: "capture_failed")
        }
    }
}
