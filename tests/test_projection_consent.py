"""MediaProjection consent must not be re-requested on every app launch.

The bug this pins down: ``MainActivity.onCreate`` read a ``proj_code`` flag
from SharedPreferences and, if it was present, launched the setup activity
1.5 seconds later — which unconditionally fired the system consent dialog.
Because an Android 10+ projection token is single-use, the projection stops on
its own while the flag lives on forever, so every launch produced a prompt for
consent that was no longer valid.

Consent genuinely cannot be persisted on Android. What *can* be prevented is
asking when a projection is already live, and asking on every launch.

These tests read the shipped Kotlin. A build machine is not available here, so
the assertions are structural — they will catch a reintroduced auto-launch or
a flag that is written but never cleared.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "agent_android/app/src/main/java/com/outcome/c2"


def kotlin(name: str) -> str:
    path = SRC / name
    assert path.is_file(), f"{name} missing from the agent"
    return path.read_text()


def test_projection_state_exists():
    """One source of truth for 'is a projection live right now'."""
    src = kotlin("ProjectionState.kt")
    assert "object ProjectionState" in src
    assert "fun isActive()" in src
    assert "MediaProjection?" in src


def test_consent_flag_is_never_used_to_decide_whether_to_prompt():
    """The stored token must not be the thing that answers 'are we capturing'.

    That is the whole bug: the flag outlives the single-use token, so trusting
    it re-prompts forever.
    """
    setup = kotlin("MediaProjectionSetupActivity.kt")
    assert "ProjectionState.isActive()" in setup, \
        "the setup activity must check for a live projection first"
    assert 'getInt("proj_code"' not in setup, \
        "the setup activity must not trust the stored consent code"


def test_setup_activity_short_circuits_when_already_live():
    setup = kotlin("MediaProjectionSetupActivity.kt")
    early = setup.split("createScreenCaptureIntent")[0]
    assert "ProjectionState.isActive()" in early, \
        "the check has to happen before the consent dialog is requested"
    assert "finish()" in early


def test_main_activity_does_not_auto_launch_the_setup():
    """The specific regression: a postDelayed auto-launch on every open.

    The button's own click handler is allowed — and necessary. What must not
    exist is a launch that happens on its own, without a tap.
    """
    main = kotlin("MainActivity.kt")
    on_create = main.split("override fun onCreate")[1].split("private fun requestBatteryExemption")[0]

    occurrences = on_create.count("MediaProjectionSetupActivity::class.java")
    assert occurrences == 1, (
        "onCreate references the consent activity "
        f"{occurrences} time(s); only the button's click handler may"
    )

    # The single reference must sit inside the button's click lambda.
    block = on_create.split('btn("4.')[1].split('btn("5.')[0]
    assert "MediaProjectionSetupActivity::class.java" in block, \
        "the only reference should be the capture button"

    assert "postDelayed" not in on_create, \
        "no deferred launch of the consent activity on startup"
    assert 'getInt("proj_code"' not in on_create


def test_projection_stop_clears_the_stored_consent():
    """Otherwise the stale flag returns and the loop restarts."""
    service = kotlin("TelegramC2Service.kt")
    assert "ProjectionState.detach(applicationContext)" in service, \
        "the projection's onStop callback must clear the stored consent"
    assert "ProjectionState.attach(proj" in service


def test_projection_state_clears_the_prefs_it_owns():
    src = kotlin("ProjectionState.kt")
    assert 'remove("proj_code")' in src
    assert 'remove("proj_data")' in src


def test_cancelling_consent_also_clears_state():
    setup = kotlin("MediaProjectionSetupActivity.kt")
    cancel_branch = setup.split("resultCode != RESULT_OK")[1][:400]
    assert "ProjectionState.detach" in cancel_branch, \
        "a cancelled dialog must not leave state behind"


def test_setup_activity_stores_the_intent_directly():
    """It passes the live Intent to the service; the service calls
    getMediaProjection. Storing a flattened uri and rebuilding it is what made
    the token unusable."""
    setup = kotlin("MediaProjectionSetupActivity.kt")
    assert 'putExtra("proj_data", data)' in setup
    assert "toUri" not in setup, \
        "the result Intent must be passed intact, not flattened to a string"
    assert 'putString("proj_data"' not in setup


def test_capture_button_reports_live_state():
    main = kotlin("MainActivity.kt")
    assert "btn4" in main
    assert "ProjectionState.isActive()" in main.split('btn("4.')[1][:400], \
        "the button should say so when a projection is already running"
    assert "fun renderProjectionState()" in main


def test_btn_helper_returns_its_view():
    """renderProjectionState() can only work if btn() hands the view back."""
    main = kotlin("MainActivity.kt")
    assert "fun btn(label: String, action: () -> Unit): Button" in main
    assert "btn4 = btn(\"4." in main


def test_projection_state_is_not_used_to_bypass_consent():
    """isActive() must reflect a live projection only.

    Guard against the shortcut version of this change, where the holder is
    populated as soon as consent is granted rather than when the projection is
    actually created.
    """
    service = kotlin("TelegramC2Service.kt")
    init = service.split("private fun initProjectionFromIntent")[1][:2000]
    attach_at = init.index("ProjectionState.attach")
    assert "getMediaProjection" in init[:attach_at], \
        "attach must happen after a projection is obtained, not before"
