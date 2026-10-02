/* Shared helpers: fetch, toasts, modal, formatting. */

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
    const u = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(1)} ${u[i]}`;
  }

  document.addEventListener("DOMContentLoaded", () => {
    const backdrop = document.getElementById("modal-backdrop");
    if (backdrop) backdrop.addEventListener("click", closeModal);
  });

  return { apiFetch, apiJson, toast, openModal, closeModal, escapeHtml, fmtBytes };
})();

window.C2 = C2;
