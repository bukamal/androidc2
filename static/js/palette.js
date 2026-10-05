/* Command palette + fuzzy search + keyboard navigation.
 *
 * A dashboard with 45 commands and 3 panes needs one entry point. Cmd/Ctrl-K
 * opens it from anywhere; it searches commands, devices and navigation targets
 * at once, scores them, and runs the winner on Enter.
 *
 * No dependencies. Scoring is subsequence matching with bonuses for
 * word-boundary hits and consecutive runs, which is enough to make
 * "dev info" find "device_info".
 */

(() => {
  const C2 = window.C2;

  let open = false;
  let items = [];
  let cursor = 0;
  let input = null;
  let list = null;
  let hint = null;
  let lastFocus = null;

  // ─── fuzzy scoring ──────────────────────────────────────────────────────

  // Returns a score, or -1 when the needle is not a subsequence of the
  // haystack. Higher is better.
  function score(haystack, needle) {
    if (!needle) return 0;
    const h = haystack.toLowerCase();
    const n = needle.toLowerCase();

    const exact = h.indexOf(n);
    if (exact === 0) return 1000;                 // prefix
    if (exact > 0) {
      // Word-boundary start is nearly as good as a prefix.
      const boundary = h[exact - 1] === " " || h[exact - 1] === "_" ||
                       h[exact - 1] === "-" || h[exact - 1] === ".";
      return boundary ? 800 - exact : 600 - exact;
    }

    // Subsequence: walk the haystack, reward consecutive hits.
    let hi = 0, hits = 0, streak = 0, best = 0, total = 0;
    for (const ch of n) {
      if (ch === " ") continue;
      const at = h.indexOf(ch, hi);
      if (at === -1) return -1;
      streak = at === hi ? streak + 1 : 0;
      best = Math.max(best, streak);
      total += streak * 2 + 1;
      hits++;
      hi = at + 1;
    }
    if (!hits) return -1;
    return Math.min(500, total + best * 10 - h.length * 0.1);
  }

  // ─── item construction ──────────────────────────────────────────────────

  function iconFor(kind) {
    return { command: "›", device: "▣", action: "⚡", nav: "◧" }[kind] || "·";
  }

  function buildItems() {
    const S = window.__c2State;
    if (!S) return [];

    const out = [];
    const seen = new Set();

    // Commands — use the device-scoped catalogue so the palette never offers
    // something the selected device would refuse.
    const catalog = (S.scopedCatalog && Object.keys(S.scopedCatalog).length)
      ? S.scopedCatalog
      : (S.catalog || {});

    for (const [type, info] of Object.entries(catalog)) {
      const key = `cmd:${type}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({
        kind: "command",
        icon: iconFor("command"),
        title: type,
        subtitle: info.desc || "",
        hint: (info.args || []).join(" ") || "no args",
        run: () => window.__c2PromptCommand(type, info),
        _t: type.toLowerCase(),
        _s: (info.desc || "").toLowerCase(),
      });
    }

    // Devices
    for (const d of S.devices || []) {
      out.push({
        kind: "device",
        icon: iconFor("device"),
        title: d.model || d.device_id,
        subtitle: [d.device_id, d.ip_address].filter(Boolean).join("  ·  "),
        hint: d.is_online ? "online" : "offline",
        state: d.is_online ? "ok" : "off",
        run: () => window.__c2OpenDevice(d),
        _t: (d.model || "").toLowerCase(),
        _s: [d.device_id, d.ip_address, d.manufacturer, ...(d.tags || [])]
              .join(" ").toLowerCase(),
      });
    }

    // Navigation / quick actions
    const nav = [
      { title: "Refresh devices",   hint: "r", run: () => window.__c2RefreshDevices() },
      { title: "Activity feed",     hint: "clear", run: () => C2.clearActivity() },
      { title: "Verify audit chain", hint: "integrity", run: () => window.__c2VerifyAudit() },
      { title: "Copy agent URL",    hint: "clipboard",
        run: () => navigator.clipboard?.writeText(window.__c2AgentUrl()) },
      { title: "Toggle view",       hint: "list / grid", run: () => window.__c2ToggleView() },
      { title: "Toggle density",    hint: "compact",     run: () => window.__c2ToggleDensity() },
    ];
    for (const n of nav) {
      out.push({
        kind: "action",
        icon: iconFor("action"),
        title: n.title,
        subtitle: "",
        hint: n.hint,
        run: n.run,
        _t: n.title.toLowerCase(),
        _s: n.hint.toLowerCase(),
      });
    }

    return out;
  }

  // ─── render ─────────────────────────────────────────────────────────────

  function filter(query) {
    const q = query.trim();
    if (!q) return items.slice(0, 60);

    const scored = [];
    for (const it of items) {
      const a = score(it._t, q);
      const b = it._s ? score(it._s, q) : -1;
      if (a < 0 && b < 0) continue;
      // Title hits beat subtitle hits.
      scored.push({ it, s: Math.max(a, b < 0 ? -1 : b - 120) });
    }
    scored.sort((x, y) => y.s - x.s);
    return scored.slice(0, 60).map(x => x.it);
  }

  function renderList(results, query) {
    list.innerHTML = "";

    if (!results.length) {
      list.innerHTML = `<li class="pal-empty">
          no match for <code>${C2.escapeHtml(query)}</code>
        </li>`;
      return;
    }

    let lastKind = null;
    results.forEach((it, i) => {
      if (it.kind !== lastKind && i > 0) {
        const h = document.createElement("li");
        h.className = "pal-group";
        h.textContent = it.kind;
        list.appendChild(h);
      }
      lastKind = it.kind;

      const li = document.createElement("li");
      li.className = `pal-item kind-${it.kind}`;
      li.dataset.index = String(i);
      li.innerHTML = `
        <span class="pal-icon">${it.icon}</span>
        <span class="pal-text">
          <span class="pal-title">${C2.escapeHtml(it.title)}</span>
          ${it.subtitle ? `<span class="pal-sub">${C2.escapeHtml(it.subtitle)}</span>` : ""}
        </span>
        <span class="pal-hint">${C2.escapeHtml(it.hint || "")}</span>`;
      li.addEventListener("click", () => choose(i));
      li.addEventListener("mousemove", () => setCursor(i));
      list.appendChild(li);
    });
    setCursor(0);
  }

  function visible() {
    return Array.from(list.querySelectorAll(".pal-item"));
  }

  function setCursor(i) {
    const rows = visible();
    if (!rows.length) return;
    cursor = Math.max(0, Math.min(i, rows.length - 1));
    rows.forEach((r, n) => r.classList.toggle("on", n === cursor));
    const active = rows[cursor];
    if (active) active.scrollIntoView({ block: "nearest" });
  }

  function choose(i) {
    const results = filter(input.value);
    const it = results[i ?? cursor];
    if (!it) return;
    close();
    // Let the palette unmount before the command's own prompt appears.
    setTimeout(() => it.run(), 0);
  }

  // ─── lifecycle ──────────────────────────────────────────────────────────

  function mount() {
    if (document.getElementById("palette")) return;

    const wrap = document.createElement("div");
    wrap.id = "palette";
    wrap.className = "palette-wrap";
    wrap.setAttribute("aria-hidden", "true");
    wrap.innerHTML = `
      <div class="palette-backdrop" data-close></div>
      <div class="palette" role="dialog" aria-modal="true" aria-label="Command palette">
        <div class="palette-bar">
          <span class="palette-caret">›</span>
          <input id="palette-input" type="text" autocomplete="off" spellcheck="false"
                 placeholder="search commands, devices, actions…"
                 aria-controls="palette-list" aria-autocomplete="list">
          <kbd class="palette-esc">esc</kbd>
        </div>
        <ul id="palette-list" class="palette-list" role="listbox"></ul>
        <div class="palette-foot">
          <span><kbd>↑</kbd><kbd>↓</kbd> move</span>
          <span><kbd>↵</kbd> run</span>
          <span><kbd>esc</kbd> close</span>
          <span class="palette-count"></span>
        </div>
      </div>`;
    document.body.appendChild(wrap);

    input = wrap.querySelector("#palette-input");
    list = wrap.querySelector("#palette-list");

    input.addEventListener("input", () => {
      renderList(filter(input.value), input.value);
    });
    input.addEventListener("keydown", onKey);
    wrap.querySelector("[data-close]").addEventListener("click", close);
  }

  function onKey(e) {
    if (e.key === "ArrowDown") { e.preventDefault(); setCursor(cursor + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setCursor(cursor - 1); }
    else if (e.key === "Home") { e.preventDefault(); setCursor(0); }
    else if (e.key === "End") { e.preventDefault(); setCursor(visible().length - 1); }
    else if (e.key === "Enter") { e.preventDefault(); choose(); }
    else if (e.key === "Escape") { e.preventDefault(); close(); }
    else if (e.key === "Tab") {
      // Keep focus inside the dialog.
      e.preventDefault();
    }
  }

  function show(prefill) {
    mount();
    lastFocus = document.activeElement;
    items = buildItems();
    open = true;

    const wrap = document.getElementById("palette");
    wrap.classList.add("on");
    wrap.setAttribute("aria-hidden", "false");

    input.value = prefill || "";
    renderList(filter(input.value), input.value);
    input.focus();
    input.select();
  }

  function close() {
    if (!open) return;
    open = false;
    const wrap = document.getElementById("palette");
    if (wrap) {
      wrap.classList.remove("on");
      wrap.setAttribute("aria-hidden", "true");
    }
    if (lastFocus && lastFocus.focus) {
      try { lastFocus.focus(); } catch (_) {}
    }
  }

  function toggle() { open ? close() : show(); }

  document.addEventListener("keydown", e => {
    const combo = (e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K");
    if (combo) {
      e.preventDefault();
      toggle();
      return;
    }
    // "/" focuses the global search, like every other tool.
    if (e.key === "/" && !open && !/^(INPUT|TEXTAREA)$/.test(e.target.tagName)) {
      const search = document.getElementById("global-search");
      if (search) {
        e.preventDefault();
        search.focus();
      }
    }
    if (e.key === "Escape" && open) close();
  });

  window.C2Palette = { show, close, toggle, score, filter };
})();
