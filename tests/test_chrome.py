"""Alignment invariants for the restructured chrome.

The topbar was "irregular" because five elements derived their height from
different combinations of padding, font-size and border — 32, 33, 34 and
34 px — and flex merely centred them, so nothing shared a baseline. A
screenshot assertion cannot see that; these checks can.

The rule the whole bar obeys: every interactive element in #topbar is exactly
`--ctl-md` tall. The same applies to the tab strip's buttons and the section
headers, each with its own token.
"""

import re
from pathlib import Path

import pytest

from test_layout import CSS, HTML, base_rule, children_of, tracks_for


def _strip_at_rules(css: str, names=("media", "supports")) -> str:
    """Remove @media / @supports blocks, honouring nested braces.

    Without this a responsive override such as
    ``@media (max-width:1400px){ .search-wrap{max-width:320px} }`` reads as
    the *only* rule for .search-wrap, and the base `height` silently
    disappears — which is exactly the sort of false failure this file is
    meant to avoid.
    """
    out = []
    i = 0
    n = len(css)
    while i < n:
        at = re.compile(r"@(" + "|".join(names) + r")\b").match(css, i)
        if not at:
            # Skip whole comments so a "@media" inside one is not matched.
            if css.startswith("/*", i):
                end = css.find("*/", i + 2)
                i = n if end == -1 else end + 2
                continue
            out.append(css[i])
            i += 1
            continue

        out.append(" ")                      # keep offsets roughly stable
        brace = css.find("{", i)
        if brace == -1:
            break
        depth = 0
        j = brace
        while j < n:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        i = j + 1
    return "".join(out)


_BASE_CSS = _strip_at_rules(CSS.read_text())


def rule(selector: str, scope: str | None = None) -> str:
    """Merged declaration body for `selector`, ignoring @media blocks.

    Later blocks override earlier ones per property, matching the cascade for
    same-specificity selectors.
    """
    css = _BASE_CSS if scope is None else scope
    merged = {}
    # The selector must be the whole one in its group: preceded by a rule
    # boundary or a comma, and followed by `{`, `,` or a pseudo-class.
    # Without the anchor, `.tstat` also matches `body[density] .tstat` and
    # the compact-mode override silently becomes the base value.
    # Walk `selector-list { ... }` so a rule written as
    # `.sec-head h2, .sidebar-head h2 { }` resolves for either member.
    for match in re.finditer(r"([^{}]+)\{([^}]*)\}", css, re.S):
        group = match.group(1)
        if not any(part.strip() == selector for part in group.split(",")):
            continue
        for decl_body in re.finditer(r"([-a-z]+)\s*:\s*([^;}]+)",
                                     match.group(2), re.S):
            merged[decl_body.group(1).strip()] = decl_body.group(2).strip()
        continue
    return "; ".join(k + ": " + v for k, v in merged.items())


def _unused_rule(selector: str, scope: str | None = None) -> str:
    """Superseded by the selector-list aware implementation above."""
    css = _BASE_CSS if scope is None else scope
    merged = {}
    pattern = r"(?:^|[},\n;])\s*" + re.escape(selector) + r"\s*(?=[{:,])"
    for match in re.finditer(pattern + r"\{([^}]*)\}", css, re.S):
        for decl_body in re.finditer(r"([-a-z]+)\s*:\s*([^;}]+)",
                                     match.group(1), re.S):
            merged[decl_body.group(1).strip()] = decl_body.group(2).strip()
    return "; ".join(k + ": " + v for k, v in merged.items())


def media_blocks(max_width: str) -> str:
    """Every @media (max-width: N) block, merged.

    More than one exists: an older rule and the v3 override for the same
    breakpoint. Taking only the first would read the superseded one.
    """
    css = CSS.read_text()
    marker = f"@media (max-width: {max_width})"
    bodies = []
    pos = 0
    while True:
        start = css.find(marker, pos)
        if start == -1:
            break
        brace = css.find("{", start)
        if brace == -1:
            break
        depth, j = 0, brace
        while j < len(css):
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        bodies.append(css[brace + 1:j])
        pos = j + 1
    return "\n".join(bodies)


def decl(body: str, prop: str) -> str | None:
    found = re.search(re.escape(prop) + r"\s*:\s*([^;}]+)", body)
    return found.group(1).strip() if found else None


def _all_root_blocks() -> str:
    css = CSS.read_text()
    return "\n".join(m.group(1) for m in
                      re.finditer(r":root\s*\{([^}]*)\}", css, re.S))


def token(name: str) -> str:
    """Resolve a custom property from every :root block (last wins)."""
    found = re.search(r"--" + re.escape(name) + r"\s*:\s*([^;}]+)",
                      _all_root_blocks())
    assert found, "--" + name + " is not declared in :root"
    return found.group(1).strip()


# ------------------------------------------------------------------ tokens

@pytest.mark.parametrize("name", [
    "ctl-sm", "ctl-md", "ctl-lg",     # control heights
    "fs-2xs", "fs-xs", "fs-sm", "fs-md", "fs-lg", "fs-xl", "fs-2xl",  # type
    "sp-1", "sp-2", "sp-3", "sp-4", "sp-5", "sp-6",  # spacing
    "lh-tight", "lh-body",
])
def test_scale_token_exists(name):
    assert token(name)


def test_control_heights_decrease():
    assert _px(token("ctl-sm")) < _px(token("ctl-md")) < _px(token("ctl-lg"))


def test_type_scale_increases():
    sizes = [_px(token(n)) for n in
             ("fs-2xs", "fs-xs", "fs-sm", "fs-md", "fs-lg", "fs-xl", "fs-2xl")]
    assert sizes == sorted(sizes), f"type scale is not monotonic: {sizes}"
    assert len(set(sizes)) == len(sizes), "two steps share a size"


def test_spacing_is_a_four_pixel_grid():
    for name in ("sp-1", "sp-2", "sp-3", "sp-4", "sp-5", "sp-6"):
        assert _px(token(name)) % 4 == 0, f"--{name} is off the 4px grid"


def _px(value: str) -> float:
    """Resolve a length to px, following one level of var() indirection."""
    value = (value or "").strip()
    match = re.match(r"var\(--([a-z0-9-]+)\)", value)
    if match:
        return _px(token(match.group(1)))
    match = re.match(r"([\d.]+)px", value)
    assert match, f"expected a px length or a var() for one, got {value!r}"
    return float(match.group(1))


# ----------------------------------------------------------------- topbar

TOPBAR_CONTROLS = [
    (".brand", "identity block"),
    (".search-wrap", "search field"),
    (".palette-trigger", "palette trigger"),
    (".tstat", "counter chip"),
    (".topbar-actions button", "icon button"),
]


@pytest.mark.parametrize("selector,what", TOPBAR_CONTROLS)
def test_topbar_control_height_comes_from_the_token(selector, what):
    """Padding-derived heights are what made the bar irregular."""
    body = rule(selector)
    assert body, f"{selector} ({what}) has no rule at all"
    height = decl(body, "height")
    assert height == "var(--ctl-md)", (
        f"{selector} ({what}) height is {height!r}; it must be "
        f"var(--ctl-md) = {token('ctl-md')}"
    )


def test_topbar_row_height_matches_its_grid_track():
    tracks = tracks_for("#app")[0]
    topbar_height = _px(decl(rule("#topbar"), "height"))
    assert tracks[0] == f"{int(topbar_height)}px", (
        f"#app reserves {tracks[0]} but #topbar is {topbar_height}px tall"
    )


def test_topbar_is_a_grid_of_zones():
    """Explicit columns stop the middle zone from shoving the controls off."""
    columns = decl(rule("#topbar"), "grid-template-columns")
    assert columns, "#topbar is not a grid"
    tracks = [t.strip() for t in columns.split()]
    assert tracks[0] == "auto"
    assert "minmax(0" in tracks[1], f"zone 2 must be able to shrink: {tracks}"
    assert tracks[-1] == "auto"


def test_topbar_children_are_zones():
    kids = [k.lstrip(".") for k in children_of("topbar")]
    assert kids, "#topbar has no in-flow children"
    # brand · search+palette · stats · actions
    assert len(kids) == 4, f"expected four zones, got {kids}"
    assert kids == ["brand", "topbar-center", "topbar-stats", "topbar-actions"], \
        f"zone order or membership changed: {kids}"


def test_search_field_is_a_real_search_input():
    html = HTML.read_text()
    assert 'id="global-search" type="search"' in html, \
        "type=search gives correct mobile keyboard and an Escape affordance"
    assert 'id="search-clear"' in html
    assert 'id="search-count"' in html
    assert 'aria-label="Search devices"' in html


def test_search_decoration_is_css_not_an_emoji():
    """An emoji glyph has different metrics on every platform, so the icon
    sits at a different height from the text."""
    assert "search-icon" in HTML.read_text()
    body = rule(".search-icon")
    assert decl(body, "position") == "absolute"
    assert "border-radius" in body, "the glyph should be drawn, not typed"
    assert not re.search(r"search-icon[^<]*>\s*[🔍]", HTML.read_text()), \
        "an emoji was left in place of the drawn icon"


def test_search_input_has_room_for_its_overlays():
    padding = decl(rule("#global-search"), "padding-inline")
    assert padding, "#global-search must reserve space for the icon and count"
    assert "32px" in padding, f"icon needs 32px inset, got {padding!r}"


def test_counters_use_the_same_height_as_the_controls():
    chip = decl(rule(".tstat"), "height")
    button = decl(rule(".topbar-actions button"), "height")
    assert chip == button, f"chips {chip} vs buttons {button}"


def test_counter_labels_are_present_for_each_value():
    html = HTML.read_text()
    for value in ("stat-online", "stat-total", "stat-cmds", "stat-files"):
        assert f'id="{value}"' in html


# ------------------------------------------------------------------- tabs

def test_tabs_are_a_segmented_control():
    body = rule(".tabs")
    assert decl(body, "display") == "flex"
    assert decl(body, "align-items") == "stretch", \
        "tabs must share one height instead of being sized by their labels"
    assert decl(body, "position") == "relative", \
        "the indicator is positioned against the strip"


def test_every_tab_button_shares_one_height():
    body = rule(".tabs button")
    height = decl(body, "height")
    assert height, ".tabs button has no fixed height"
    assert height == "44px"
    assert decl(body, "flex") == "1 1 0", \
        "flex:1 makes all tabs the same width whatever the label length"
    assert decl(body, "display") == "flex"
    assert decl(body, "align-items") == "center"


def test_tab_labels_are_wrapped_so_the_badge_can_sit_beside():
    html = HTML.read_text()
    tabs = len(re.findall(r'data-tab="', html))
    assert html.count('<span class="tab-label">') == tabs, \
        "every tab needs its label wrapped for the flex row to align"
    assert 'class="tab-indicator"' in html


def test_tab_indicator_is_derived_not_measured():
    """--i / --n means it cannot drift from the layout or need JS measuring."""
    css = CSS.read_text()
    body = rule(".tab-indicator")
    assert decl(body, "position") == "absolute"
    width = decl(body, "width")
    assert "var(--n)" in width, f"indicator width must come from --n: {width}"
    transform = decl(body, "transform")
    assert "var(--i)" in transform, f"indicator offset must come from --i: {transform}"
    assert decl(body, "transition"), "the indicator should animate"


def test_tab_indicator_is_mirrored_for_rtl():
    """The page is dir=rtl, so the offset runs the other way."""
    css = CSS.read_text()
    rtl = re.search(r'\[dir="rtl"\]\s+\.tab-indicator\s*\{([^}]*)\}', css, re.S)
    assert rtl, "no rtl override for the tab indicator"
    transform = decl(rtl.group(1), "transform")
    assert "--i) * -100%" in transform, \
        f"rtl transform should negate the offset, got {transform!r}"


def test_tabs_declare_their_count():
    html = HTML.read_text()
    strip = re.search(r'id="device-tabs"[^>]*style="([^"]*)"', html)
    assert strip, "#device-tabs is missing"
    assert "--i:" in strip.group(1) and "--n:" in strip.group(1), \
        "the strip must ship a default --i/--n so the indicator is placed " \
        "before the first click"
    buttons = len(re.findall(r'data-tab="', html))
    declared = re.search(r"--n:(\d+)", strip.group(1))
    assert int(declared.group(1)) == buttons, \
        f"--n says {declared.group(1)} but there are {buttons} tabs"


def test_tabs_are_accessible():
    html = HTML.read_text()
    assert 'role="tablist"' in html
    tabs = len(re.findall(r'data-tab="', html))
    assert html.count('role="tab"') == tabs
    assert tabs >= 8, f"expected the results tab alongside the original 7, got {tabs}"
    assert 'aria-selected="true"' in html
    assert 'class="tab-badge" id="tab-badge-commands"' in html


def test_js_drives_the_indicator_from_the_index():
    src = (Path(__file__).resolve().parent.parent
           / "static/js/dashboard.js").read_text()
    assert 'setProperty("--n"' in src
    assert 'setProperty("--i"' in src
    assert 'aria-selected' in src, "the tab state must be announced, not just painted"
    assert "renderCatalog()" in src.split("const selectTab")[1][:800], \
        "switching to the commands tab should refresh it"


# ---------------------------------------------------------------- headings

def test_section_heading_is_one_component():
    html = HTML.read_text()
    assert html.count('class="sec-head') >= 4, \
        "sidebar, activity, queue and audit should all use the same header"
    assert '<h2 class="sec-title">' in html


def test_sec_head_has_a_fixed_height_and_a_single_type_size():
    body = rule(".sec-head")
    assert decl(body, "height"), ".sec-head must have a fixed height"
    assert decl(body, "display") == "flex"
    assert decl(body, "align-items") == "center"

    # One member of the group is enough: the reader resolves groups.
    for title_selector in (".sec-head h2", ".sidebar-head h2"):
        title = rule(title_selector)
        assert title, f"{title_selector} has no rule"
        assert decl(title, "font-size") == "var(--fs-xs)"
        assert decl(title, "line-height") == "1", \
            "a heading with line-height 1 aligns with the row beside it"
        assert decl(title, "margin") == "0"


def test_headings_do_not_hard_code_font_sizes():
    """The old heading rules each carried their own px size."""
    css = CSS.read_text()
    for selector in (".sec-title", ".sec-head h2", ".sidebar-head h2",
                     ".brand-name", ".brand-sub", ".tstat span", ".tstat label"):
        body = rule(selector)
        if not body:
            continue
        size = decl(body, "font-size") or ""
        assert size.startswith("var(--fs"), \
            f"{selector} font-size is {size!r}; use the type scale"


def test_legacy_heading_classes_still_resolve():
    """panel.js and older templates may still use .sidebar-head."""
    assert ".sidebar-head" in CSS.read_text()


# -------------------------------------------------------------- responsive

@pytest.mark.parametrize("width,selector", [
    ("1400px", ".search-wrap"),
    ("1200px", ".topbar-stats"),
    ("1000px", ".topbar-stats"),
    ("760px", ".brand-text"),
])
def test_narrow_viewports_have_a_declared_strategy(width, selector):
    body = media_blocks(width)
    assert body, f"no {width} breakpoint"
    assert selector in body, \
        f"at {width} the strategy should involve {selector}"


def test_search_narrows_before_the_counters_do():
    """Order matters: at 1400px the field shrinks, at 1200px the counters go.
    Losing the field first would be backwards."""
    assert ".search-wrap" in media_blocks("1400px")
    assert ".topbar-stats" in media_blocks("1200px")


def test_compact_density_shrinks_every_bar_the_same_way():
    """rule() returns declarations, so each bar is queried separately."""
    for target, what in ((".tabs button", "tab strip"),
                         (".sec-head", "section headers"),
                         (".tstat", "counter chips")):
        base_height = decl(rule(target), "height")
        compact_height = decl(rule('body[data-density="compact"] ' + target), "height")

        assert base_height, f"{what} has no base height"
        assert compact_height, f"compact density must give {what} an explicit height"
        assert _px(compact_height) < _px(base_height), (
            f"{what}: compact is {compact_height} and base is {base_height} "
            f"— compact mode is not compact"
        )


def test_logical_properties_are_used_for_the_rtl_layout():
    """The page is dir=rtl; left/right assumptions are what produce the
    half-broken alignment."""
    # Only the elements that place themselves against an edge need logical
    # properties; .tabs and .search-wrap are plain flex containers.
    for selector in (".tab-indicator", ".sec-head", ".search-icon",
                     ".search-clear", ".search-count", "#global-search",
                     ".palette-trigger", ".tstat"):
        body = rule(selector)
        assert body, f"{selector} has no base rule"
        assert "inline-start" in body or "inline-end" in body or \
               "padding-inline" in body, \
            f"{selector} should use logical properties for rtl"


def test_no_physical_direction_survives_in_the_chrome():
    """`left:`/`right:` in an rtl document mirrors the wrong way round."""
    for selector in (".search-icon", ".search-clear", ".search-count",
                     ".tab-indicator"):
        body = rule(selector)
        for physical in ("left:", "right:", "margin-left", "margin-right"):
            assert physical not in body, \
                f"{selector} uses {physical}; use the logical equivalent"
