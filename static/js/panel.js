/* Shared helpers: fetch, toasts, modal, formatting, activity feed. */

const C2 = (() => {
  async function apiFetch(url, opts = {}) {
    const headers = Object.assign(
      { "Content-Type": "application/json" },
      opts.headers || {},
    );
    return fetch(url, { ...opts, headers });
  }

  async function apiJson(url, opts = {}) {
    const r = await apiFetch(url, opts);
    const text = await r.text();
    try { return JSON.parse(text); }
    catch (_) { return text; }
  }

  function toast(message, kind = "info", ttl = 3500) {
    const stack = document.getElementById("toast-stack");
    if (!stack) return;
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = message;
    stack.appendChild(el);
    setTimeout(() => {
      el.classList.add("fade-out");
      setTimeout(() => el.remove(), 400);
    }, ttl);
  }

  // ═══════════ ACTIVITY FEED ═══════════
  const ACTIVITY_MAX = 120;

  function activity(icon, message, kind = "info") {
    const feed = document.getElementById("activity-feed");
    if (!feed) return;
    const now = new Date();
    const t = now.toTimeString().slice(0, 8);

    const el = document.createElement("div");
    el.className = `activity-item ${kind}`;
    el.innerHTML = `
      <span class="time">${t}</span>
      <span class="icon">${icon}</span>
      <span class="msg">${escapeHtml(message)}</span>
    `;

    // insert at top
    feed.insertBefore(el, feed.firstChild);

    // trim
    while (feed.children.length > ACTIVITY_MAX) {
      feed.removeChild(feed.lastChild);
    }
  }

  function clearActivity() {
    const feed = document.getElementById("activity-feed");
    if (feed) feed.innerHTML = "";
  }

  function openModal(html) {
    const wrap = document.getElementById("modal-wrap");
    const modal = document.getElementById("modal");
    if (!wrap || !modal) return;
    modal.innerHTML = html;
    wrap.classList.remove("hidden");
  }

  function closeModal() {
    const wrap = document.getElementById("modal-wrap");
    if (wrap) wrap.classList.add("hidden");
  }

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

  function fmtBytes(n) {
    if (!n) return "0 B";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(1)} ${u[i]}`;
  }

  function fmtRelative(iso) {
    if (!iso) return "—";
    const then = new Date(iso).getTime();
    const diff = Math.floor((Date.now() - then) / 1000);
    if (diff < 5) return "now";
    if (diff < 60) return `${diff}s`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
    return `${Math.floor(diff / 86400)}d`;
  }

  function batteryClass(pct) {
    if (pct <= 15) return "critical";
    if (pct <= 35) return "low";
    return "";
  }

  document.addEventListener("DOMContentLoaded", () => {
    const backdrop = document.getElementById("modal-backdrop");
    if (backdrop) backdrop.addEventListener("click", closeModal);

    const clearBtn = document.getElementById("btn-clear-activity");
    if (clearBtn) clearBtn.addEventListener("click", clearActivity);
  });

  return {
    apiFetch, apiJson, toast,
    activity, clearActivity,
    openModal, closeModal,
    escapeHtml, fmtBytes, fmtRelative, batteryClass,
  };
})();

window.C2 = C2;
