/* Live vitals: KPI tiles with sparklines.
 *
 * The topbar already shows counts. What it does not show is *trend* — whether
 * the fleet is growing, whether commands are completing, how long they take.
 * These tiles are a rolling window over that, drawn as inline SVG so there is
 * no chart library and no extra request.
 */

(() => {
  const C2 = window.C2;

  const MAX = 40;                      // samples retained per tile
  const series = new Map();            // key -> number[]
  const last = new Map();              // key -> most recent value

  // ─── sparkline ──────────────────────────────────────────────────────────

  function pathFor(values, w, h) {
    if (!values.length) return "";
    if (values.length === 1) {
      // No leading space: keep the path string uniform with the multi-point
      // branch so it can be pattern-matched in tests.
      return `M0,${(h / 2).toFixed(1)}L${w},${(h / 2).toFixed(1)}`;
    }
    const lo = Math.min(...values);
    const hi = Math.max(...values);
    const span = hi - lo || 1;
    const step = w / (values.length - 1);
    let d = "";
    values.forEach((v, i) => {
      const x = (i * step).toFixed(2);
      const y = (h - ((v - lo) / span) * (h - 2) - 1).toFixed(2);
      d += `${i ? "L" : "M"}${x},${y}`;
    });
    return d;
  }

  function spark(values, { w = 72, h = 20, tone = "accent" } = {}) {
    const d = pathFor(values, w, h);
    const id = `g${Math.random().toString(36).slice(2, 8)}`;
    if (!d) return `<svg class="spark" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}"></svg>`;
    return `
      <svg class="spark tone-${tone}" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}"
           preserveAspectRatio="none" aria-hidden="true">
        <defs>
          <linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%"  stop-color="currentColor" stop-opacity="0.28"/>
            <stop offset="100%" stop-color="currentColor" stop-opacity="0"/>
          </linearGradient>
        </defs>
        <path d="${d}L${w},${h}L0,${h}Z" fill="url(#${id})" stroke="none"/>
        <path d="${d}" fill="none" stroke="currentColor" stroke-width="1.4"
              stroke-linejoin="round" stroke-linecap="round"/>
      </svg>`;
  }

  // ─── sampling ───────────────────────────────────────────────────────────

  function push(key, value) {
    if (typeof value !== "number" || !isFinite(value)) return;
    const arr = series.get(key) || [];
    arr.push(value);
    if (arr.length > MAX) arr.shift();
    series.set(key, arr);
    last.set(key, value);
  }

  function sample() {
    const S = window.__c2State;
    if (!S) return;

    const devices = S.devices || [];
    const online = devices.filter(d => d.is_online).length;
    const ws = devices.filter(d => d.ws_online).length;
    const queued = (S.queue || []).length;

    push("devices", devices.length);
    push("online", online);
    push("queue", queued);

    // Command completion latency, measured client-side from issue to result.
    const started = performance.now();
    fetch("/api/devices", { credentials: "same-origin", cache: "no-store" })
      .then(() => push("latency", Math.round(performance.now() - started)))
      .catch(() => {});
  }

  // ─── rendering ──────────────────────────────────────────────────────────

  function tile({ key, label, tone = "accent", unit = "" }) {
    const v = last.get(key);
    const arr = series.get(key) || [];
    const delta = arr.length > 1 ? arr[arr.length - 1] - arr[arr.length - 2] : 0;
    return `
      <div class="vital" data-vital="${key}" tabindex="0">
        <div class="vital-top">
          <span class="vital-label">${label}</span>
          <span class="vital-delta ${delta > 0 ? "up" : delta < 0 ? "down" : ""}">
            ${delta ? (delta > 0 ? "▲" : "▼") + Math.abs(delta) : ""}
          </span>
        </div>
        <div class="vital-value">
          ${v === undefined ? "–" : v}<span class="vital-unit">${unit}</span>
        </div>
        ${spark(arr, { tone })}
        <div class="vital-bar"><i style="width:${Math.min(100, (arr.length / MAX) * 100)}%"></i></div>
      </div>`;
  }

  function render() {
    const host = document.getElementById("vitals");
    if (!host) return;
    host.innerHTML = [
      tile({ key: "devices", label: "fleet" }),
      tile({ key: "online",  label: "online", tone: "ok" }),
      tile({ key: "queue",   label: "queue",  tone: "warn" }),
      tile({ key: "latency", label: "api",    tone: "alt", unit: "ms" }),
    ].join("");
  }

  function start(intervalMs = 5000) {
    render();
    sample();
    render();
    setInterval(() => { sample(); render(); }, intervalMs);
  }

  window.C2Vitals = { start, sample, render, push, spark };
})();
