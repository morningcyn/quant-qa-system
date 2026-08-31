// 导出：高清长图 PNG / 图像式多页 PDF / pywebview 保存对话框
(function () {
  "use strict";

  const A4_RATIO = 297 / 210; // 高宽比
  const PAGE_W = 794; // 对应 A4 宽度 px（96dpi）

  function isPywebview() {
    return !!(window.pywebview && window.pywebview.api);
  }

  async function canvasOf(el) {
    return html2canvas(el, {
      scale: 2,
      backgroundColor: window.Theme.colors().bg, // 跟随当前主题，浅色导出为浅底
      useCORS: false,
      logging: false,
    });
  }

  function base64ToBytes(b64) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  }

  async function saveOrDownload(b64, fileName) {
    if (isPywebview()) {
      const path = await window.pywebview.api.save_file(fileName, b64);
      return path ? `已保存至 ${path}` : null;
    }
    const a = document.createElement("a");
    a.href = `data:application/octet-stream;base64,${b64}`;
    a.download = fileName;
    a.click();
    return `已下载 ${fileName}`;
  }

  // 导出前预处理：展开折叠面板、加 .exporting 类
  function prepare(root) {
    const container = root.closest(".page") || root;
    container.classList.add("exporting");
    container.querySelectorAll(".collapse").forEach((c) => c.classList.add("open"));
    return container;
  }

  function cleanup(container) {
    container.classList.remove("exporting");
  }

  // 高清长图 PNG
  async function exportPNG(root, fileName) {
    const container = prepare(root);
    try {
      const canvas = await canvasOf(container);
      const b64 = canvas.toDataURL("image/png").split(",")[1];
      return await saveOrDownload(b64, fileName);
    } finally {
      cleanup(container);
    }
  }

  // 图像式多页 PDF（绕开中文字体嵌入）
  async function exportPDF(root, fileName) {
    const container = prepare(root);
    try {
      const canvas = await canvasOf(container);
      const fullW = canvas.width;
      const fullH = canvas.height;
      const pageH = Math.round(fullW * A4_RATIO);
      const { jsPDF } = window.jspdf;
      const pdf = new jsPDF({ unit: "px", format: "a4", orientation: "portrait" });
      const pageCount = Math.max(1, Math.ceil(fullH / pageH));
      for (let i = 0; i < pageCount; i++) {
        if (i > 0) pdf.addPage();
        const sliceCanvas = document.createElement("canvas");
        sliceCanvas.width = fullW;
        const sliceH = Math.min(pageH, fullH - i * pageH);
        sliceCanvas.height = sliceH;
        const ctx = sliceCanvas.getContext("2d");
        ctx.drawImage(canvas, 0, i * pageH, fullW, sliceH, 0, 0, fullW, sliceH);
        const img = sliceCanvas.toDataURL("image/jpeg", 0.92);
        pdf.addImage(img, "JPEG", 0, 0, 794, (sliceH / fullW) * 794);
      }
      const b64 = pdf.output("datauristring").split(",")[1];
      return await saveOrDownload(b64, fileName);
    } finally {
      cleanup(container);
    }
  }

  window.Exporter = { exportPNG, exportPDF, saveOrDownload, isPywebview };
})();
