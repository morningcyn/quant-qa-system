// 深浅色主题单例：data-theme 属性 + localStorage 持久化 + 图表调色板注入
// 约定：<html data-theme="light"> 触发 theme.css 的浅色变量覆盖；默认（无属性）为暗色。
// 图表为 canvas 渲染，option 无法写 var() —— colors() 用 getComputedStyle 读 CSS 变量，
// 供 charts.js / emotion_card.js 在模块加载时取色。切换后 reload 全量重绘（hash 路由保留）。
(function () {
  "use strict";

  const KEY = "theme";

  function isLight() {
    return (document.documentElement.dataset.theme || "dark") === "light";
  }

  // head 内联脚本已保证首帧主题，这里兜底（如浏览器恢复页面场景）
  function apply() {
    const saved = localStorage.getItem(KEY);
    if (saved === "light" || saved === "dark") {
      document.documentElement.dataset.theme = saved;
    }
  }

  function toggle() {
    localStorage.setItem(KEY, isLight() ? "dark" : "light");
    location.reload(); // 图表全量重绘用新主题，简单可靠
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  // 图表调色板：两主题统一结构，派生色按主题分支（浅色下加深基色/提高 alpha 保可见）
  function colors() {
    const light = isLight();
    const c = {
      text: cssVar("--text"), text2: cssVar("--text-2"), text3: cssVar("--text-3"),
      grid: cssVar("--border"), axis: cssVar("--axis"),
      dBlue: cssVar("--d-blue"), sTeal: cssVar("--s-teal"),
      good: cssVar("--good"), warning: cssVar("--warning"), critical: cssVar("--critical"),
      bg: cssVar("--bg"), surface2: cssVar("--surface-2"),
      riskLight: light ? "#c22e2e" : "#e46a6a", // 情绪曲线风险标签色（暗色需亮红，浅色需深红）
    };
    if (light) {
      c.tooltip = { bg: "#ffffff", border: "#cfcfc9", text: "#1b1b19", shadow: "0 6px 24px rgba(0,0,0,0.15)" };
      c.band = {
        good: "rgba(14,148,20,0.10)", goodLabel: "rgba(14,148,20,0.75)",
        mild: "rgba(181,116,0,0.08)", mildLabel: "rgba(181,116,0,0.7)",
        bad: "rgba(194,46,46,0.11)", badLabel: "rgba(194,46,46,0.75)",
      };
      c.areaTop = "rgba(28,111,201,0.18)";
      c.areaBottom = "rgba(28,111,201,0.02)";
      c.areaLine = "rgba(28,111,201,0.4)";   // 助理节点垂直虚线
      c.trendArea = "rgba(28,111,201,0.10)"; // 趋势图面积填充
      c.splitArea = "rgba(0,0,0,0.025)";     // 雷达网格条带
      c.axisPointer = "#cfcfc9";             // 趋势图十字光标标签
    } else {
      c.tooltip = { bg: "#21211f", border: "#383835", text: "#ffffff", shadow: "0 6px 24px rgba(0,0,0,0.45)" };
      c.band = {
        good: "rgba(12,163,12,0.07)", goodLabel: "rgba(12,163,12,0.6)",
        mild: "rgba(250,178,25,0.05)", mildLabel: "rgba(250,178,25,0.55)",
        bad: "rgba(208,59,59,0.09)", badLabel: "rgba(208,59,59,0.6)",
      };
      c.areaTop = "rgba(57,135,229,0.28)";
      c.areaBottom = "rgba(57,135,229,0.02)";
      c.areaLine = "rgba(57,135,229,0.35)";
      c.trendArea = "rgba(57,135,229,0.10)";
      c.splitArea = "rgba(255,255,255,0.02)";
      c.axisPointer = "#383835";
    }
    c.dotBorder = "#ffffff"; // 曲线/节点描边：两主题均为白
    return c;
  }

  // 切换按钮（body 尾加载，DOM 已就绪）：dark 下显示 ☀️（点击变浅色），light 下显示 🌙
  const btn = document.getElementById("btn-theme");
  if (btn) {
    btn.textContent = isLight() ? "🌙" : "☀️";
    btn.addEventListener("click", toggle);
  }

  apply();
  window.Theme = { KEY, isLight, apply, toggle, colors };
})();
