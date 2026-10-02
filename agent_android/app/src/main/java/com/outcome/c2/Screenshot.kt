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

object Screenshot {

    /**
     * Capture using a live MediaProjection instance.
     * MediaProjection is created once by C2Service and reused.
     */
    fun capture(ctx: Context, projection: MediaProjection): JSONObject {
        val wm = ctx.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val metrics = DisplayMetrics()
        @Suppress("DEPRECATION")
        wm.defaultDisplay.getRealMetrics(metrics)
        val width = metrics.widthPixels
        val height = metrics.heightPixels
        val density = metrics.densityDpi

        val reader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 2)
        val vd: VirtualDisplay = projection.createVirtualDisplay(
            "c2-screen", width, height, density,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            reader.surface, null, null
        )

        val latch = CountDownLatch(1)
        var bmp: Bitmap? = null
        val handler = Handler(Looper.getMainLooper())

        reader.setOnImageAvailableListener({ r ->
            val image = r.acquireLatestImage() ?: return@setOnImageAvailableListener
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
            } finally {
                image.close()
                latch.countDown()
            }
        }, handler)

        latch.await(4, TimeUnit.SECONDS)
        reader.setOnImageAvailableListener(null, null)
        try { vd.release() } catch (_: Exception) {}

        val out = bmp ?: return JSONObject().put("error", "capture_timeout")
        val baos = ByteArrayOutputStream()
        out.compress(Bitmap.CompressFormat.JPEG, 85, baos)
        val b64 = Base64.encodeToString(baos.toByteArray(), Base64.NO_WRAP)
        return JSONObject()
            .put("format", "jpeg")
            .put("width", width)
            .put("height", height)
            .put("data_b64", b64)
    }
}
