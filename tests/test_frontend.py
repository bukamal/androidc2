"""Frontend logic that has real behaviour worth testing.

The palette's fuzzy scorer and the vitals sparkline generator are the only
parts of the new UI where a bug would be silent: a wrong score runs the wrong
command, and a malformed SVG path renders a blank chart. Both are pure
functions, so they are exercised directly under node against the *shipped*
source — a test that re-implements them would prove nothing.

The JS is assembled with concatenation rather than f-strings on purpose: the
payloads are full of braces, and an f-string would try to evaluate them.

Skipped when node is unavailable.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PALETTE = ROOT / "static/js/palette.js"
VITALS = ROOT / "static/js/vitals.js"
CSS = ROOT / "static/css/dashboard.css"
HTML = ROOT / "templates/index.html"
DASH = ROOT / "static/js/dashboard.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not installed")


def run_node(script: str) -> str:
    r = subprocess.run(["node", "-e", script],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def load_function(path: Path, name: str, params: str) -> str:
    """Pull one function body out of a shipped source file.

    Intentionally brittle: if the signature changes this fails, rather than
    silently testing a stale copy.
    """
    src = path.read_text()
    pattern = re.compile(r"function " + name + r"\(" + re.escape(params) +
                         r"\) \{[\s\S]*?\n  \}")
    match = pattern.search(src)
    assert match, name + "(" + params + ") not found in " + path.name
    return "(" + match.group(0).replace("function " + name, "function") + ")"


SCORE_FN = load_function(PALETTE, "score", "haystack, needle")
PATH_FN = load_function(VITALS, "pathFor", "values, w, h")


# ------------------------------------------------------------- fuzzy score

CASES = [
    ("device_info", "dev info", True),
    ("device_info", "device_info", True),
    ("device_info", "dvi", True),
    ("inject_payload", "payload", True),
    ("mic_record", "mic", True),
    ("self_destruct", "self", True),
    ("camera_photo", "phot", True),
    ("list_dir", "sms", False),
    ("camera_photo", "zzzz", False),
    ("vibrate", "xxxx", False),
    ("", "anything", False),
    ("SM-S918B", "s918", True),
    ("SM-S918B", "samsung", False),
    # "brat" is a literal substring of "vibrate" — matching it is correct.
    ("vibrate", "brat", True),
]


def test_fuzzy_scorer_matches_the_right_commands():
    script = "\n".join([
        "const score = " + SCORE_FN + ";",
        "const cases = " + json.dumps(CASES) + ";",
        "const bad = [];",
        "for (const c of cases) {",
        "  const got = score(c[0], c[1]) >= 0;",
        "  if (got !== c[2]) bad.push(JSON.stringify(c));",
        "}",
        "console.log(JSON.stringify(bad));",
        "process.exit(bad.length ? 1 : 0);",
    ])
    bad = json.loads(run_node(script) or "[]")
    assert not bad, "scorer disagreed on: " + ", ".join(bad)


def test_scorer_ranks_stronger_matches_higher():
    """A full prefix must outrank a mid-string hit.

    Note: `score("device_info", "device")` also scores 1000, because "device"
    is itself a prefix — that is correct, not a tie to break.
    """
    script = "\n".join([
        "const score = " + SCORE_FN + ";",
        "console.log(JSON.stringify([",
        "  score('device_info', 'device_info'),",   # prefix at 0
        "  score('device_info', 'info'),",          # word boundary
        "  score('device_info', 'vice_info'),",     # mid-string
        "]));",
    ])
    prefix, boundary, mid = json.loads(run_node(script))
    assert prefix > boundary > mid > 0


def test_subsequence_matches_always_rank_below_real_ones():
    """A loose character walk is capped so it can never outrank a real hit."""
    script = "\n".join([
        "const score = " + SCORE_FN + ";",
        "console.log(JSON.stringify([",
        "  score('screenshot', 'scrsht'),",        # loose subsequence
        "  score('device_info', 'info'),",         # real match",
        "]));",
    ])
    loose, real = json.loads(run_node(script))
    assert loose >= 0, "expected the subsequence to match at all"
    assert real > loose, "a subsequence match must not outrank a boundary match"


def test_scorer_is_case_insensitive():
    script = "\n".join([
        "const score = " + SCORE_FN + ";",
        "console.log(JSON.stringify([",
        "  score('Device_Info', 'dev'),",
        "  score('DEVICE_INFO', 'DEV'),",
        "  score('Device_Info', 'DEVICE'),",
        "]));",
    ])
    values = json.loads(run_node(script))
    assert values[0] == values[1] == values[2]


def test_empty_needle_scores_zero_and_matches():
    script = "\n".join([
        "const score = " + SCORE_FN + ";",
        "console.log(JSON.stringify([score('anything', ''), score('', '')]));",
    ])
    values = json.loads(run_node(script))
    assert values == [0, 0]


def test_scorer_rejects_unrelated_and_out_of_order_input():
    """A false positive is worse than no match: it runs the wrong command."""
    script = "\n".join([
        "const score = " + SCORE_FN + ";",
        "console.log(JSON.stringify([",
        "  score('abc', 'cba'),",
        "  score('list_dir', 'dirl'),",
        "  score('vibrate', 'bvat'),",
        "  score('device_info', 'zzzzzz'),",
        "  score('sms_list', 'xmail'),",
        "]));",
    ])
    results = json.loads(run_node(script))
    assert all(r < 0 for r in results), f"out-of-order input matched: {results}"


def test_scorer_returns_a_real_number_not_a_boolean():
    script = "\n".join([
        "const score = " + SCORE_FN + ";",
        "console.log(JSON.stringify(typeof score('device_info', 'dev')));",
    ])
    assert json.loads(run_node(script)) == "number"


# -------------------------------------------------------------- sparkline

PATH_CASES = [
    [],
    [1],
    [5, 5, 5],
    [1, 2, 3, 4, 5],
    [9, 4, 7, 1, 8, 2],
    list(range(40)),
    [0, 0, 1],
]


def _paths(values, w=72, h=20):
    script = "\n".join([
        "const pathFor = " + PATH_FN + ";",
        "console.log(JSON.stringify(pathFor(" + json.dumps(values) + ", " +
        str(w) + ", " + str(h) + ")));",
    ])
    return json.loads(run_node(script))


@pytest.mark.parametrize("values", PATH_CASES, ids=lambda v: str(v)[:18])
def test_sparkline_path_is_well_formed(values):
    path = _paths(values)
    if not path:
        # Only an empty series may produce an empty path.
        assert values == [], "non-empty series produced no path: " + str(values)
        return
    assert re.fullmatch(
        r"M-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?"
        r"(?:L-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?)+",
        path,
    ), "malformed path for " + str(values) + ": " + repr(path)


def test_sparkline_handles_a_flat_series_without_dividing_by_zero():
    path = _paths([7, 7, 7, 7])
    assert path
    ys = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", path)][1::2]
    assert len(set(ys)) == 1, "a flat series should stay flat"


def test_sparkline_stays_inside_the_viewbox():
    path = _paths([9, 4, 7, 1, 8, 2], w=72, h=20)
    coords = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", path)]
    xs, ys = coords[0::2], coords[1::2]
    assert min(xs) >= 0 and max(xs) <= 72
    assert min(ys) >= 0 and max(ys) <= 20


def test_sparkline_spans_the_full_width_for_a_long_series():
    path = _paths(list(range(40)), w=72, h=20)
    xs = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", path)][0::2]
    assert xs[0] == 0
    assert abs(xs[-1] - 72) < 0.01


# ------------------------------------------------------------ integration

def test_palette_and_vitals_expose_their_api():
    palette = PALETTE.read_text()
    assert "window.C2Palette" in palette
    for fn in ("show", "close", "toggle"):
        assert ("function " + fn + "(") in palette
    assert "window.C2Palette = { show, close, toggle" in palette
    assert "window.C2Vitals" in VITALS.read_text()
    assert "C2Vitals.start" in DASH.read_text()


def test_palette_opens_on_the_keyboard_combo():
    src = PALETTE.read_text()
    assert "(e.metaKey || e.ctrlKey)" in src
    assert 'e.key === "k" || e.key === "K"' in src
    assert "e.preventDefault()" in src


def test_palette_handles_arrow_and_escape_keys():
    src = PALETTE.read_text()
    for key in ("ArrowDown", "ArrowUp", "Enter", "Escape"):
        assert 'e.key === "' + key + '"' in src, key + " not handled"


def test_palette_clamps_the_cursor():
    src = PALETTE.read_text()
    assert "Math.max(0, Math.min(i, rows.length - 1))" in src


def test_palette_falls_back_to_the_full_catalogue():
    """A device that declared nothing must still offer every command."""
    src = PALETTE.read_text()
    assert "S.scopedCatalog && Object.keys(S.scopedCatalog).length" in src
    assert "S.catalog || {}" in src


def test_palette_escapes_device_supplied_text():
    """Device names are attacker-influenced; they reach innerHTML."""
    src = PALETTE.read_text()
    for site in ("C2.escapeHtml(it.title)", "C2.escapeHtml(it.subtitle)",
                 "C2.escapeHtml(it.hint"):
        assert site in src, site + " missing — unescaped device text in the DOM"


def test_vitals_tiles_are_labelled_and_tone_marked():
    src = VITALS.read_text()
    for label in ("fleet", "online", "queue", "api"):
        assert 'label: "' + label + '"' in src
    assert 'tone: "ok"' in src and 'tone: "warn"' in src and 'tone: "alt"' in src


def test_vitals_bounds_its_history():
    """Unbounded series would grow the DOM on a long-lived tab."""
    src = VITALS.read_text()
    assert "MAX = 40" in src
    assert "arr.shift()" in src


def test_html_wires_every_new_asset():
    html = HTML.read_text()
    for needle in ("/static/js/palette.js", "/static/js/vitals.js",
                   'id="vitals"', 'id="btn-palette"', 'id="btn-density"',
                   'data-density="comfortable"'):
        assert needle in html, needle + " missing from index.html"
    assert html.index("/static/js/palette.js") < html.index("/static/js/dashboard.js"), \
        "palette reads state that dashboard publishes, so it must load after panel/vitals"


def test_css_defines_the_new_surfaces():
    css = CSS.read_text()
    for selector in (".palette-wrap", ".palette-list", ".pal-item",
                     ".vitals", ".vital", ".palette-trigger",
                     'body[data-density="compact"]', "prefers-reduced-motion"):
        assert selector in css, selector + " missing from the stylesheet"


def test_reduced_motion_shortens_animations_rather_than_hiding_things():
    css = CSS.read_text()
    block = css.split("prefers-reduced-motion")[1][:500]
    assert "animation-duration" in block
    assert "display: none" not in block


def test_dashboard_publishes_state_for_the_palette():
    src = DASH.read_text()
    assert "window.__c2State" in src
    for fn in ("__c2PromptCommand", "__c2OpenDevice", "__c2RefreshDevices",
               "__c2ToggleDensity", "__c2ToggleView", "__c2VerifyAudit"):
        assert fn in src, fn + " not exported to the palette"


def test_command_results_raise_a_toast():
    """A command that finishes while you are on another tab should still be
    visible."""
    src = DASH.read_text()
    block = src.split('socket.on("command_result"')[1][:600]
    assert "C2.toast(" in block


def test_nothing_new_is_added_to_the_python_api():
    """The UI upgrade must not quietly widen the attack surface."""
    from app import create_app

    app = create_app(start_background=False)
    routes = {r.rule for r in app.url_map.iter_rules()}
    before = {
        "/", "/login", "/logout", "/api/session", "/api/devices",
        "/api/catalog", "/api/audit", "/api/operators",
    }
    assert routes >= before, "expected routes went missing"
    assert not any(r.startswith("/api/vitals") for r in routes), \
        "the UI upgrade added an endpoint it did not need"
