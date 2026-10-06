"""static/js/results.js — the result viewer.

The other frontend suites are structural (they assert markup and wiring). This
one executes the module's logic against a stub DOM, because the things that
break in this file are all runtime behaviours: a payload that is not JSON, a
512 KB payload freezing the tab, results leaking across a device switch.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static/js/results.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _node(*args, **kw):
    return subprocess.run(["node", *args], capture_output=True, text=True,
                          timeout=30, **kw)


@pytest.fixture(scope="module")
def harness():
    """Runs results.js under node with a minimal DOM and exposes its helpers."""
    return _harness()


def _harness():
    preload = f"""
      const C2 = {{
        escapeHtml: s => String(s == null ? "" : s)
          .replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;",
            '"':"&quot;","'":"&#39;"}}[c])),
        fmtBytes: n => (n || 0) + " B",
        fmtRelative: () => "now",
        apiFetch: async (url, opts) => {{
          if (!globalThis.__fetch) throw new Error("no fetch stub");
          return globalThis.__fetch(url, opts);
        }},
        apiJson: async (url, opts) => {{
          const r = await C2.apiFetch(url, opts);
          return JSON.parse(await r.text());
        }},
        toast: (m) => {{ globalThis.__toast = m; }},
      }};
      globalThis.C2 = C2;
      const nodes = {{}};
      function mk(id) {{
        return nodes[id] || (nodes[id] = {{
          id, innerHTML: "", textContent: "", hidden: false, disabled: false,
          className: "", title: "", dataset: {{}},
          addEventListener() {{}}, removeAttribute() {{}},
          classList: {{ add() {{}}, remove() {{}}, contains: () => false }},
        }});
      }}
      const IDS = ["result-list","result-head","result-body","result-copy",
                   "result-download","result-waiting","mirror-grab",
                   "mirror-path","mirror-path-refresh"];
      globalThis.document = {{
        querySelector: sel => mk(sel.replace("#","")),
        getElementById: id => mk(id),
        createElement: () => mk("tmp"),
        addEventListener() {{}},
      }};
      globalThis.window = {{ addEventListener() {{}} }};
      globalThis.state = {{ current: null }};
      window.__c2State = state;
      globalThis.navigator = {{}};
      globalThis.URL = {{ createObjectURL: () => "blob:x",
                          revokeObjectURL() {{}} }};
      globalThis.Blob = function() {{}};
      globalThis.__timers = [];
      globalThis.setTimeout = (fn) => {{ globalThis.__timers.push(fn); return 0; }};
      globalThis.__drain = async () => {{
        for (let i = 0; i < 5 && globalThis.__timers.length; i++) {{
          const batch = globalThis.__timers.splice(0);
          for (const fn of batch) await fn();
        }}
      }};
      globalThis.__nodes = nodes;
    """
    # The IIFE keeps its helpers private on purpose. Drive it through the DOM
    # it writes into and the public window.__c2Results it exposes, which is
    # what the browser actually uses.
    src = JS.read_text()
    path = Path("/tmp/opencode/_results_harness.js")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(preload + "\n" + src)
    res = _node("--input-type=commonjs", "-e", f"require({str(path)!r})", cwd=str(ROOT))
    assert res.returncode == 0, res.stderr
    return path


def run_js(body: str):
    """Runs a snippet with the harness already loaded and the module booted."""
    path = Path("/tmp/opencode/_results_run.js")
    path.write_text(
        f'require({str(HARNESS)!r});\n(async () => {{\n{body}\n}})();\n'
    )
    res = _node(str(path))
    assert res.returncode == 0, res.stdout + res.stderr
    return res.stdout.strip()


def render(cmd):
    """Feeds one result in and returns the rendered list HTML."""
    return run_js(f"""
      const R = globalThis.window.__c2Results;
      globalThis.state = window.__c2State;
      window.__c2State.current = {{ id: 1 }};
      R.reset();
      R.record({json.dumps(cmd)});
      console.log(__nodes["result-list"].innerHTML + "\u0000" +
                  __nodes["result-body"].textContent);
    """)


HARNESS = Path("/tmp/opencode/_results_harness.js")


# ------------------------------------------------------------- result parsing

def _split(rendered):
    listing, _, body = rendered.partition("\u0000")
    return listing, body


def test_plain_string_result_is_not_lost(harness):
    """The terminal used to JSON.parse unguarded; plain text threw there."""
    listing, body = _split(render({
        "id": 1, "device_id": 1, "command_type": "shell",
        "status": "done", "result": "not json at all",
    }))
    assert "not json at all" in body


def test_json_string_result_is_parsed(harness):
    _, body = _split(render({
        "id": 1, "device_id": 1, "command_type": "shell",
        "status": "done", "result": '{"stdout":"hi"}',
    }))
    assert json.loads(body) == {"stdout": "hi"}


def test_empty_result_renders_as_empty_not_null(harness):
    _, body = _split(render({
        "id": 1, "device_id": 1, "command_type": "device_info",
        "status": "done", "result": "",
    }))
    assert "empty result" in body
    assert "null" not in body


def test_error_is_the_first_thing_an_operator_sees(harness):
    listing, _ = _split(render({
        "id": 1, "device_id": 1, "command_type": "screenshot",
        "status": "failed", "result": '{"error":"no_capture_path"}',
    }))
    assert "no_capture_path" in listing


def test_summary_takes_only_the_first_line(harness):
    listing, _ = _split(render({
        "id": 1, "device_id": 1, "command_type": "shell",
        "status": "done", "result": '{"stdout":"line1\\nline2"}',
    }))
    assert "line1" in listing and "line2" not in listing


def test_huge_payload_is_capped_for_the_dom(harness):
    """A 512 KB result as one <pre> locks the tab. The stored value is fine;
    only the rendered copy is cut."""
    big = json.dumps({"files": ["x" * 32 for _ in range(20000)]})
    _, body = _split(render({
        "id": 1, "device_id": 1, "command_type": "list_dir",
        "status": "done", "result": big,
    }))
    assert len(body) < 250_000, "rendered output must stay bounded"
    assert "truncated for display" in body


def test_normal_payload_is_not_truncated(harness):
    _, body = _split(render({
        "id": 1, "device_id": 1, "command_type": "device_info",
        "status": "done", "result": '{"ok":true,"n":1}',
    }))
    assert "truncated" not in body


def test_history_is_bounded(harness):
    """Every result lives in memory; an unbounded list leaks on a long session."""
    out = run_js("""
      const R = window.__c2Results;
      window.__c2State.current = { id: 1 };
      R.reset();
      for (let i = 0; i < 200; i++) {
        R.record({ id: i + 1, device_id: 1, command_type: "shell",
                   status: "done", result: "{}" });
      }
      console.log(R.history().length);
    """)
    assert int(out) <= 40, "history must not grow without bound"


def test_results_from_another_device_are_ignored(harness):
    out = run_js("""
      const R = window.__c2Results;
      window.__c2State.current = { id: 1 };
      R.reset();
      R.record({ id: 1, device_id: 2, command_type: "shell",
                 status: "done", result: "{}" });
      console.log(R.history().length);
    """)
    assert out == "0", "a payload from a different device must not appear"


def test_reset_clears_everything_on_device_switch(harness):
    """Otherwise device A's payload renders under device B's name."""
    out = run_js("""
      const R = window.__c2Results;
      window.__c2State.current = { id: 1 };
      R.reset();
      R.record({ id: 7, device_id: 1, command_type: "shell",
                 status: "done", result: "{}" });
      R.reset();
      console.log(R.history().length + "|" +
                  String(__nodes["result-body"].textContent === ""));
    """)
    assert out == "0|true"


# ------------------------------------------------------------- capture path

def test_capture_chip_reports_the_consent_free_path(harness):
    out = run_js("""
      window.__c2Results.onNewFile({ device_id: 1, category: "screenshot" });
      console.log("ok");
    """)
    assert out == "ok", "a new screenshot must refresh the view"


def test_capture_chip_explains_a_disabled_accessibility_service(harness):
    """'accessibility off' is actionable in a way 'no_capture_path' is not."""
    out = run_js("""
      globalThis.__api_status = { service_bound: false };
      window.__c2State.current = { id: 1 };
      const R = window.__c2Results;
      R.record({ id: 3, device_id: 1, command_type: "screenshot_status",
                 status: "done", result: JSON.stringify(globalThis.__api_status) });
      console.log(__nodes["result-body"].textContent);
    """)
    assert "service_bound" in out


def test_capture_chip_surfaces_the_error_verbatim(harness):
    out = run_js("""
      window.__c2State.current = { id: 1 };
      const R = window.__c2Results;
      R.record({ id: 4, device_id: 1, command_type: "screen_record",
                 status: "failed",
                 result: JSON.stringify({ error: "projection_required" }) });
      console.log(__nodes["result-body"].textContent);
    """)
    assert "projection_required" in out


# ------------------------------------------------------------------- markup

def test_markup_provides_the_ids_the_module_queries():
    """The module querySelectors these ids; a rename in the template silently
    disables the whole panel."""
    html = (ROOT / "templates/index.html").read_text()
    for node_id in ["result-list", "result-head", "result-body", "result-copy",
                    "result-download", "mirror-grab", "mirror-path"]:
        assert f'id="{node_id}"' in html, f"#{node_id} missing from the template"


def test_results_tab_and_panel_exist_together():
    html = (ROOT / "templates/index.html").read_text()
    assert 'data-tab="results"' in html
    assert 'data-panel="results"' in html
    assert 'id="tab-badge-results"' in html


def test_module_is_loaded_after_the_dom_helpers():
    """results.js calls C2.escapeHtml on first render, so it must load after
    panel.js which defines it."""
    html = (ROOT / "templates/index.html").read_text()
    assert html.index("/static/js/panel.js") < html.index("/static/js/results.js")
    assert html.index("/static/js/results.js") < html.index("/static/js/dashboard.js"), \
        "dashboard.js dispatches into __c2Results during boot"


def test_dashboard_dispatches_results_events():
    src = (ROOT / "static/js/dashboard.js").read_text()
    assert "__c2Results?.record(cmd)" in src, \
        "command_result never reaches the viewer"
    assert "__c2Results?.reset()" in src, \
        "switching devices must clear the previous device's results"
    assert "__c2Results?.onNewFile(file)" in src


def test_css_defines_the_viewer_classes():
    css = (ROOT / "static/css/dashboard.css").read_text()
    for cls in [".results-split", ".result-list", ".result-body",
                ".result-item", ".path-chip"]:
        assert cls in css, f"{cls} has no styling, so the panel renders raw"

def test_capture_chip_is_driven_by_the_status_endpoint(harness):
    """refreshCapturePath is public, so the chip logic is reachable after all.

    This is the piece that tells the operator *why* a capture failed, which is
    the whole reason the endpoint exists.
    """
    out = run_js("""
      const R = window.__c2Results;
      window.__c2State.current = { id: 1 };
      globalThis.__api = { status: { source: "accessibility" } };
      globalThis.__fetch = async () => ({ text: async () => JSON.stringify(globalThis.__api) });
      await R.refreshCapturePath();
      await globalThis.__drain();
      console.log(__nodes["mirror-path"].textContent + "|" +
                  __nodes["mirror-path"].className);
    """)
    text, cls = out.split("|")
    assert text == "accessibility"
    assert "ok" in cls, "the consent-free path must read as healthy"


def test_capture_chip_names_the_disabled_service(harness):
    """'accessibility off' is actionable; 'no_capture_path' is not."""
    out = run_js("""
      const R = window.__c2Results;
      window.__c2State.current = { id: 1 };
      globalThis.__fetch = async () => ({
        text: async () => JSON.stringify({ status: { service_bound: false } })
      });
      await R.refreshCapturePath();
      await globalThis.__drain();
      console.log(__nodes["mirror-path"].textContent + "|" +
                  __nodes["mirror-path"].className);
    """)
    text, cls = out.split("|")
    assert text == "accessibility off"
    assert "bad" in cls


def test_capture_chip_shows_the_raw_error(harness):
    out = run_js("""
      const R = window.__c2Results;
      window.__c2State.current = { id: 1 };
      globalThis.__fetch = async () => ({
        text: async () => JSON.stringify({ status: { error: "projection_required" } })
      });
      await R.refreshCapturePath();
      await globalThis.__drain();
      console.log(__nodes["mirror-path"].textContent + "|" +
                  __nodes["mirror-path"].className);
    """)
    text, cls = out.split("|")
    assert text == "projection_required"
    assert "bad" in cls


def test_capture_request_surfaces_refusal(harness):
    """A queued-command refusal (unsupported, unknown device) must reach the
    operator rather than fail silently."""
    out = run_js("""
      const R = window.__c2Results;
      window.__c2State.current = { id: 1 };
      globalThis.__toast = "";
      globalThis.__fetch = async () => ({
        status: 200,
        text: async () => JSON.stringify({ error: "unknown_command" })
      });
      await R.grabNow();
      await globalThis.__drain();
      console.log(globalThis.__toast);
    """)
    assert "unknown_command" in out


def test_capture_without_a_device_is_a_no_op(harness):
    out = run_js("""
      const R = window.__c2Results;
      window.__c2State.current = null;
      let called = false;
      globalThis.__fetch = async () => { called = true; return {}; };
      await R.grabNow();
      await globalThis.__drain();
      console.log(String(called));
    """)
    assert out == "false", "no device selected must not fire a request"
