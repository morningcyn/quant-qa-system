// SPA 骨架：hash 路由、布局、Toast/Modal 工具、错误处理
(function () {
  "use strict";

  const app = document.getElementById("app");

  const routes = [
    { pattern: /^#\/assistants$/, view: () => Views.assistants.render() },
    { pattern: /^#\/assistant\/(\d+)$/, view: (m) => Views.assistantDetail.render(Number(m[1])) },
    { pattern: /^#\/report\/(\d+)$/, view: (m) => Views.report.render(Number(m[1])) },
    { pattern: /^#\/settings$/, view: () => Views.settings.render() },
  ];

  // ---------- Toast ----------
  function toast(message, type) {
    type = type || "info";
    const root = document.getElementById("toast-root");
    const el = document.createElement("div");
    el.className = `toast toast-${type}`;
    el.textContent = message;
    root.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transition = "opacity 0.3s";
      setTimeout(() => el.remove(), 320);
    }, type === "error" ? 5200 : 3000);
  }

  // ---------- Modal ----------
  function openModal({ title, content, footer, width }) {
    const root = document.getElementById("modal-root");
    const mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.innerHTML = `
      <div class="modal ${width === "lg" ? "modal-lg" : ""}">
        <div class="modal-head">
          <div class="modal-title"></div>
          <div class="modal-close">✕</div>
        </div>
        <div class="modal-body"></div>
      </div>`;
    mask.querySelector(".modal-title").textContent = title || "";
    const bodyEl = mask.querySelector(".modal-body");
    if (typeof content === "string") bodyEl.innerHTML = content;
    else if (content) bodyEl.appendChild(content);
    if (footer) bodyEl.appendChild(footer);
    const close = () => mask.remove();
    mask.querySelector(".modal-close").addEventListener("click", close);
    mask.addEventListener("mousedown", (e) => { if (e.target === mask) close(); });
    root.appendChild(mask);
    return { el: mask, body: bodyEl, close };
  }

  // ---------- 错误提示 ----------
  function handleError(err, fallback) {
    const msg = (err && err.message) || fallback || "操作失败，请重试";
    toast(msg, "error");
  }

  // ---------- 工具 ----------
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtDate(s) {
    return s ? String(s).slice(0, 16) : "-";
  }

  window.UI = { toast, openModal, handleError, esc, fmtDate };

  // ---------- 路由 ----------
  // 页面内容加载后，浏览器可能因窗口初始化/内容替换发生滚动漂移（视口停在页面中部，导致
  // 报告页"只有标题、分数显示不出来"）。渲染后的短窗口内：只要用户没有任何输入且页面被
  // 自动滚动，就强制拉回顶部；用户一旦主动滚动/点击即放行，绝不抢控制权。
  function suppressScrollDrift() {
    let hasUserInput = false;
    ["wheel", "touchstart", "keydown", "mousedown"].forEach((t) =>
      window.addEventListener(t, () => { hasUserInput = true; }, { passive: true })
    );
    const timer = setInterval(() => {
      if (!hasUserInput && window.scrollY > 2) window.scrollTo(0, 0);
    }, 400);
    setTimeout(() => clearInterval(timer), 8000);
  }

  function render() {
    // hash 导航（如详情页滚动后点"查看报告"）不重置滚动位置，这里强制回到顶部，避免新页面从中间开始显示
    window.scrollTo(0, 0);
    suppressScrollDrift();
    const hash = location.hash || "#/assistants";
    for (const r of routes) {
      const m = hash.match(r.pattern);
      if (m) {
        document.querySelectorAll("#topnav a").forEach((a) => a.classList.remove("active"));
        const navKey = hash.startsWith("#/assistants") ? "assistants" : hash.startsWith("#/settings") ? "settings" : null;
        if (navKey) {
          const link = document.querySelector(`#topnav a[data-nav="${navKey}"]`);
          if (link) link.classList.add("active");
        }
        return r.view(m);
      }
    }
    location.hash = "#/assistants";
  }

  window.addEventListener("hashchange", render);
  document.getElementById("brand-home").addEventListener("click", () => { location.hash = "#/assistants"; });
  render();
})();
