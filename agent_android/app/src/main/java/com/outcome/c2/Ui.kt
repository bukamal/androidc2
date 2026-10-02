package com.outcome.c2

import android.content.Context
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.VibrationEffect
import android.os.Vibrator
import android.widget.Toast

object Ui {
    fun toast(ctx: Context, text: String) {
        Handler(Looper.getMainLooper()).post {
            Toast.makeText(ctx, text, Toast.LENGTH_LONG).show()
        }
    }

    fun vibrate(ctx: Context, ms: Long) {
        val v = ctx.getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            v.vibrate(VibrationEffect.createOneShot(ms, VibrationEffect.DEFAULT_AMPLITUDE))
        } else {
            @Suppress("DEPRECATION") v.vibrate(ms)
        }
    }
}
