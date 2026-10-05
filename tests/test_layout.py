"""Grid track / child count agreement.

The layout broke once already: `#app` declared `grid-template-rows: 60px 1fr`
(two tracks) and a third child — the vitals rail — was added. The extra child
fell into an implicit row, `#vitals` inherited the `1fr` and swallowed the
viewport, and `#main` was pushed underneath it. Visually: everything
overlapping.

Nothing about that failure is visible in a screenshot assertion, so it is
checked structurally instead: every grid container must declare exactly as
many row tracks as it has element children. That holds in every media query
too, which is where it broke the second time.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "templates/index.html"
CSS = ROOT / "static/css/dashboard.css"

# Elements that never participate in the parent's grid layout.
NON_GRID_CHILDREN = {"script", "style", "template", "link", "meta", "noscript"}

# `position: absolute|fixed` takes a child out of flow, so it occupies no grid
# track. #device-panel is fixed and #welcome is absolutely positioned over the
# main area — both are siblings of #main but neither is a track.
OUT_OF_FLOW_POSITIONS = ("absolute", "fixed")


# --------------------------------------------------------------- html side

class ChildScanner(HTMLParser):
    """Collect the direct element children of every id, with their classes."""

    VOID = {"img", "br", "hr", "input", "meta", "link", "source"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []                      # [(id, classes)]
        self.children = {}                   # id -> [(tag, id, classes)]

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        element_id = attrs.get("id")
        classes = (attrs.get("class") or "").split()

        if self.stack:
            parent_id = self.stack[-1][0]
            if parent_id is not None:
                self.children.setdefault(parent_id, []).append(
                    (tag, element_id, classes)
                )

        if element_id and element_id not in self.children:
            self.children[element_id] = []

        if tag not in self.VOID:
            self.stack.append((element_id, classes))

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1][0] is not None or tag not in self.VOID:
            self.stack.pop()


def _scan():
    scanner = ChildScanner()
    scanner.feed(HTML.read_text())
    return scanner.children


def _rules_for(element_id, classes):
    css = CSS.read_text()
    selectors = ["#" + element_id] if element_id else []
    selectors += ["." + c for c in classes]

    bodies = []
    for selector in selectors:
        for match in re.finditer(re.escape(selector) + r"\s*\{([^}]*)\}", css, re.S):
            bodies.append(match.group(1))
    return bodies


def _decl(bodies, prop):
    for body in bodies:
        found = re.search(re.escape(prop) + r"\s*:\s*([a-z-]+)", body)
        if found:
            return found.group(1)
    return None


def _occupies_a_track(element_id, classes) -> bool:
    """False when the stylesheet takes this element out of flow.

    Two mechanisms, both legitimate:
      * position: absolute|fixed — overlays its parent
      * display: none — hidden at this breakpoint (the 1200px query drops
        #activity-panel and narrows #main to two columns to match)
    """
    bodies = _rules_for(element_id, classes)
    position = _decl(bodies, "position")
    if position in OUT_OF_FLOW_POSITIONS:
        return False
    if _decl(bodies, "display") == "none":
        return False
    return True


def children_of(element_id: str, in_flow_only: bool = True) -> list:
    """Direct element children. By default, only the ones that occupy a grid
    track — absolutely and fixed positioned siblings are excluded."""
    kids = _scan().get(element_id, [])
    out = []
    for tag, kid_id, classes in kids:
        if tag in NON_GRID_CHILDREN:
            continue
        if in_flow_only and not _occupies_a_track(kid_id, classes):
            continue
        out.append(kid_id or ("." + classes[0] if classes else tag))
    return out


def all_children_of(element_id: str) -> list:
    return [kid_id or tag for tag, kid_id, _ in
            _scan().get(element_id, []) if tag not in NON_GRID_CHILDREN]


# ---------------------------------------------------------------- css side

def _split_tracks(value: str) -> list[str]:
    """Split a track list on top-level whitespace, respecting parens."""
    tracks, depth, current = [], 0, ""
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch.isspace() and depth == 0:
            if current:
                tracks.append(current)
                current = ""
        else:
            current += ch
    if current:
        tracks.append(current)
    return tracks


def _extract(css: str, selector: str, prop: str) -> list[str]:
    """Every value declared for `prop` on `selector`, including in media
    queries."""
    found = []
    # selector { ... } — the brace body is flat enough for this to be safe
    for match in re.finditer(
        re.escape(selector) + r"\s*\{([^}]*)\}", css, re.S
    ):
        body = match.group(1)
        # No anchor: a declaration may follow a comment ("; ... */") rather
        # than a semicolon, and `[^;}]` stops at whichever comes first.
        decl = re.search(re.escape(prop) + r"\s*:\s*([^;}]+)", body)
        if decl:
            found.append(decl.group(1).strip())
    return found


def tracks_for(selector: str, prop: str = "grid-template-rows") -> list[list[str]]:
    return [_split_tracks(v) for v in _extract(CSS.read_text(), selector, prop)]


# ------------------------------------------------------------------- tests

# #app declares its rows explicitly, which is where a missing track silently
# overlaps. #main uses implicit rows at desktop width, so it is checked
# separately.
GRID_CONTAINERS = ["app"]


@pytest.mark.parametrize("container", GRID_CONTAINERS)
def test_grid_rows_match_child_count(container):
    """Every grid container declares one row track per in-flow child."""
    kids = children_of(container)
    assert kids, f"#{container} has no in-flow children"

    declared = tracks_for("#" + container)
    assert declared, f"#{container} has children but no grid-template-rows"

    for index, tracks in enumerate(declared):
        where = "base rule" if index == 0 else f"override #{index}"
        assert len(tracks) == len(kids), (
            f"#{container} {where}: {len(tracks)} row track(s) {tracks} "
            f"but {len(kids)} in-flow child(ren) {kids}. A child with no "
            f"track lands in an implicit row and overlaps."
        )


def test_app_siblings_are_all_deliberately_positioned():
    """The three extra #app children must be out of flow on purpose.

    #device-panel is fixed, #welcome is absolutely positioned over the main
    area. If one of those ever loses its positioning it silently becomes a
    grid track and the layout breaks again.
    """
    for child in ("device-panel", "welcome"):
        assert child in all_children_of("app"), \
            f"#{child} should be a child of #app"
        assert child not in children_of("app"), \
            f"#{child} must be out of flow, or it becomes a grid track"


def test_no_empty_tracks():
    """`auto` is a valid track value; an empty slot is not."""
    for tracks in tracks_for("#app"):
        assert all(t.strip() for t in tracks), \
            f"#app: empty track in {tracks}"


def base_rule(selector: str, prop: str) -> str | None:
    """The declaration from the first (non-media) rule for `selector`.

    Media queries are deliberately out of scope here: the responsive rules
    legitimately restructure the grid together with a `display: none` on the
    pane they drop, which is checked separately below.
    """
    css = CSS.read_text()
    for match in re.finditer(re.escape(selector) + r"\s*\{([^}]*)\}", css, re.S):
        found = re.search(re.escape(prop) + r"\s*:\s*([^;}]+)", match.group(1))
        if found:
            return found.group(1).strip()
    return None


def test_main_columns_match_its_in_flow_children():
    """#main is a three-pane grid at desktop width."""
    columns = base_rule("#main", "grid-template-columns")
    assert columns, "#main declares no columns"

    tracks = _split_tracks(columns)
    kids = children_of("main")
    assert kids, "#main has no in-flow children"
    assert len(tracks) == len(kids), (
        f"#main: {len(tracks)} column(s) {tracks} but "
        f"{len(kids)} in-flow child(ren) {kids}"
    )


def test_narrow_layout_pairs_narrower_columns_with_a_hidden_pane():
    """Below 1200px #main drops to two columns and #activity-panel is hidden.

    Both halves matter: narrowing the grid without hiding the pane leaves an
    item in an implicit column, which is the overlap bug in miniature.
    """
    css = CSS.read_text()
    query = re.search(r"@media \(max-width: 1200px\) \{([\s\S]*?)\n\}", css)
    assert query, "the 1200px breakpoint is missing"

    body = query.group(1)
    columns = re.search(r"#main\s*\{[^}]*grid-template-columns\s*:\s*([^;}]+)",
                        body, re.S)
    assert columns, "1200px query does not narrow #main"

    tracks = _split_tracks(columns.group(1).strip())
    assert len(tracks) == 2, f"expected two columns below 1200px, got {tracks}"

    hidden = re.search(r"#activity-panel\s*\{[^}]*display\s*:\s*none",
                       body, re.S)
    assert hidden, \
        "#main drops to two columns but #activity-panel is still rendered — " \
        "it would fall into an implicit column"


def test_vitals_rail_is_a_direct_child_of_app():
    """It has to sit between the topbar and #main to be laid out at all."""
    kids = children_of("app")
    assert "vitals" in kids, f"#vitals is not a direct child of #app: {kids}"
    assert kids.index("topbar") < kids.index("vitals") < kids.index("main"), \
        f"document order matters for grid tracks: {kids}"


def test_main_is_the_last_child_of_app():
    """#main must own the flexible track, or the page scrolls instead of the
    panes."""
    kids = children_of("app")
    assert kids[-1] == "main", f"#main should be last, got {kids}"


def test_main_track_is_the_flexible_one():
    tracks = tracks_for("#app")[0]
    assert len(tracks) >= 3
    last = tracks[-1]
    assert "fr" in last, \
        f"the last #app track should be the flexible one, got {last!r}"


def test_flexible_track_allows_shrinking():
    """`1fr` has an implicit `minmax(auto, 1fr)` floor, so tall content pushes
    the container past the viewport instead of scrolling inside it."""
    tracks = tracks_for("#app")[0]
    last = tracks[-1]
    assert last.startswith("minmax(0") or last == "1fr", \
        f"expected minmax(0, 1fr) or 1fr, got {last!r}"
    assert "#main" in CSS.read_text() and "min-height: 0" in CSS.read_text(), \
        "#main needs min-height:0 to be allowed to shrink inside the track"


def test_topbar_height_matches_its_grid_track():
    """A track and a height that disagree leaves a dead band or a clip."""
    tracks = tracks_for("#app")[0]
    fixed = tracks[0]
    match = re.fullmatch(r"(\d+)px", fixed)
    assert match, f"first #app track should be a fixed px height, got {fixed!r}"

    css = CSS.read_text()
    bodies = re.findall(r"#topbar\s*\{([^}]*)\}", css, re.S)
    heights = []
    for body in bodies:
        decl = re.search(r"(?:^|;)\s*height\s*:\s*(\d+)px", body)
        if decl:
            heights.append(int(decl.group(1)))
    assert heights, "#topbar declares no fixed height"
    assert match.group(1) in [str(h) for h in heights], (
        f"#app reserves {match.group(1)}px for the topbar but #topbar "
        f"declares {heights}"
    )


def test_vitals_row_is_content_sized():
    tracks = tracks_for("#app")[0]
    assert tracks[1] == "auto", \
        f"the vitals track should size to its content, got {tracks[1]!r}"


def test_app_is_exactly_one_viewport_tall():
    css = CSS.read_text()
    body = re.search(r"#app\s*\{([^}]*)\}", css, re.S).group(1)
    assert "height: 100vh" in body
    assert "overflow" not in body or "overflow: visible" in body, \
        "#app must not clip; #main scrolls internally instead"


def test_vitals_cannot_stretch_its_row():
    css = CSS.read_text()
    block = re.search(r"\.vitals\s*\{([^}]*)\}", css, re.S).group(1)
    assert "min-width: 0" in block or "overflow: hidden" in block, \
        ".vitals needs containment so a wide tile cannot widen the row"
    assert "grid-template-columns" in block


def test_media_queries_keep_every_track():
    """Responsive overrides are separate rules and drift just as easily."""
    kids = children_of("app")
    css = CSS.read_text()
    queries = re.findall(r"@media[^{]*\{([\s\S]*?)\n\}", css)

    seen = 0
    for query in queries:
        for match in re.finditer(r"#app\s*\{([^}]*)\}", query):
            decl = re.search(r"grid-template-rows\s*:\s*([^;]+)", match.group(1))
            if not decl:
                continue
            seen += 1
            tracks = _split_tracks(decl.group(1).strip())
            assert len(tracks) == len(kids), (
                "a media query redefines #app with "
                f"{len(tracks)} track(s) {tracks} but #app has "
                f"{len(kids)} children {kids}"
            )
    assert seen >= 1, "no responsive #app rule found — was one removed?"


def test_palette_is_outside_the_grid():
    """It is position:fixed on body, so it cannot become a grid track."""
    html = HTML.read_text()
    assert "<div id=\"palette\"" not in html, \
        "the palette is created by JS and appended to body, not authored here"
    css = CSS.read_text()
    block = re.search(r"\.palette-wrap\s*\{([^}]*)\}", css, re.S).group(1)
    assert "position: fixed" in block
    assert "inset: 0" in block
