// 批量评分页：导入整批聊天记录 → 自动切分客户 → 后台评分 → 进度轮询 → 报告跳转
// #/batch 导入页；#/batch/{batch_id} 进度页；render 开头 clearInterval 防泄漏
(function () {
  "use strict";
  window.Views = window.Views || {};

  const POLL_MS = 2000;
  let pollTimer = null;

  function clearPoll() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  const EXCEL_EXT = /\.(xlsx|xls)$/i;

  // 文本解码：UTF-8 严格解码优先（fatal 模式，乱码即抛错），失败回退 GBK——
  // Windows 系统默认编码（Excel 另存 CSV / 记事本导出的中文文件常为 GBK，避免中文乱码）。
  function decodeBuffer(buf) {
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(buf);
    } catch (e) { /* UTF-8 非法 → 回退 GBK */ }
    try {
      return new TextDecoder("gbk").decode(buf);
    } catch (e) { /* 环境不支持 GBK → 兜底宽松 UTF-8 */ }
    return new TextDecoder("utf-8").decode(buf);
  }

  // readFile 统一返回 {text}（粘贴/CSV/JSON/普通 Excel→CSV）或 {rooms}（房间导出）。
  function readFile(file) {
    if (EXCEL_EXT.test(file.name)) {
      return readExcel(file);
    }
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        try {
          resolve({ text: decodeBuffer(new Uint8Array(reader.result)) });
        } catch (err) {
          reject(err instanceof Error ? err : new Error("文件解码失败"));
        }
      };
      reader.onerror = () => reject(new Error("文件读取失败"));
      reader.readAsArrayBuffer(file);
    });
  }

  // Excel 房间导出识别：表头含「完整聊天记录」列 → 逐行提取 rooms（每行一个房间/客户会话），
  // 每个房间独立评分任务；否则按普通表格转 CSV 走现有导入流程。
  function readExcel(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        try {
          if (typeof XLSX === "undefined") {
            throw new Error("Excel 解析组件未加载，请更新程序后重试");
          }
          // codepage: 936 兜底老版 .xls 的中文编码（GBK/CP936，Windows 系统编码）
          const wb = XLSX.read(new Uint8Array(reader.result), { type: "array", codepage: 936 });
          const sheet = wb.Sheets[wb.SheetNames[0]];
          if (!sheet) throw new Error("Excel 文件中没有可读取的工作表");
          const rows = XLSX.utils.sheet_to_json(sheet, { header: 1, raw: false });
          if (!rows.length) throw new Error("Excel 表格内容为空");
          const header = (rows[0] || []).map((h) => String(h || "").trim());
          const colFull = header.findIndex((h) => h.includes("完整聊天"));
          if (colFull >= 0) {
            // 房间模式：客户列（优先「客户列表」）+ 房间ID 组成客户名，完整聊天记录为会话文本
            let colCustomer = header.findIndex((h) => h.includes("客户列表"));
            if (colCustomer < 0) colCustomer = header.findIndex((h) => h.includes("客户"));
            const colRoomId = header.findIndex((h) => h.includes("房间ID"));
            const rooms = [];
            rows.slice(1).forEach((r) => {
              const full = String(r[colFull] || "").trim();
              if (!full) return;
              let name = colCustomer >= 0 ? String(r[colCustomer] || "").trim() : "";
              const rid = colRoomId >= 0 ? String(r[colRoomId] || "").trim() : "";
              if (rid) name = name ? `${name}（${rid}）` : `房间 ${rid}`;
              if (!name) name = `房间 ${rooms.length + 1}`;
              rooms.push({ customer_name: name, text: full });
            });
            if (!rooms.length) throw new Error("未在「完整聊天记录」列找到任何房间记录");
            resolve({ rooms });
            return;
          }
          const csv = XLSX.utils.sheet_to_csv(sheet, { FS: ",", RS: "\n" });
          if (!csv.trim()) throw new Error("Excel 表格内容为空");
          resolve({ text: csv });
        } catch (err) {
          reject(err instanceof Error ? err : new Error("Excel 转换失败"));
        }
      };
      reader.onerror = () => reject(new Error("文件读取失败"));
      reader.readAsArrayBuffer(file);
    });
  }

  const STATUS_META = {
    pending: { label: "待处理", cls: "badge-neutral" },
    processing: { label: "处理中", cls: "badge-blue" },
    retrying: { label: "重试中", cls: "badge-warning" },
    completed: { label: "已完成", cls: "badge-good" },
    failed: { label: "失败", cls: "badge-critical" },
  };

  // 客户当前情绪标签（progress 附加 current_emotion；报告页内查看完整情绪分析）
  const EMOJI = {
    "积极/认可": "😄", "中性": "😐", "担忧": "😟", "焦虑": "😰",
    "不满": "😠", "愤怒": "🤬", "失望": "😞", "怀疑": "🤨",
  };
  const EMO_LEVEL = {
    "积极/认可": "good", "中性": "neutral", "担忧": "warning", "怀疑": "warning",
    "失望": "critical", "焦虑": "critical", "不满": "critical", "愤怒": "critical",
  };

  function emotionBadge(cur) {
    if (!cur || !cur.emotion) return "";
    const cls = EMO_LEVEL[cur.emotion] || "neutral";
    return `<span class="badge badge-${cls}" title="客户当前情绪：${UI.esc(cur.emotion)}（强度 ${cur.intensity ?? "-"}）">${EMOJI[cur.emotion] || ""} ${UI.esc(cur.emotion)}</span>`;
  }

  function statusBadge(status) {
    const m = STATUS_META[status] || STATUS_META.pending;
    return `<span class="badge ${m.cls}">${m.label}</span>`;
  }

  function statGrid(stats) {
    // stats: [{label, value, extra?}] → 统计条
    return `<div style="display:flex;gap:16px;flex-wrap:wrap">
      ${stats.map((s) => `
        <div class="stat-tile">
          <div class="stat-value num">${s.value}</div>
          <div class="stat-label">${s.label}</div>
          ${s.extra ? `<div class="stat-extra">${s.extra}</div>` : ""}
        </div>`).join("")}
    </div>`;
  }

  // ---------- 导入页 ----------

  function loadHistory(container) {
    API.get("/api/batch")
      .then((data) => {
        const box = document.getElementById("batch-history");
        if (!box) return;
        if (!data.batches || data.batches.length === 0) {
          box.innerHTML = `<div class="empty">还没有批量评分记录</div>`;
          return;
        }
        box.innerHTML = `
          <table class="table">
            <thead><tr><th>批次</th><th>状态</th><th>客户数</th><th>任务</th><th>完成</th><th>失败</th><th>创建时间</th><th>操作</th></tr></thead>
            <tbody>
            ${data.batches.map((b) => {
              const st = b.stats || {};
              return `<tr>
                <td><b>${UI.esc(b.title || b.batch_id.slice(0, 8))}</b></td>
                <td>${statusBadge(b.status)}</td>
                <td class="num">${(b.source_stats.customer_count ?? 0)}</td>
                <td class="num">${st.total ?? 0}</td>
                <td class="num">${st.completed ?? 0}</td>
                <td class="num">${st.failed ?? 0}</td>
                <td class="muted num">${UI.fmtDate(b.created_at)}</td>
                <td style="white-space:nowrap">
                  <a class="btn btn-ghost btn-sm" href="#/batch/${b.batch_id}">查看</a>
                  <button class="btn btn-ghost btn-sm btn-danger-ghost" data-batch-del="${b.batch_id}" data-batch-title="${UI.esc(b.title || b.batch_id.slice(0, 8))}">删除</button>
                </td>
              </tr>`;
            }).join("")}
            </tbody>
          </table>`;
        box.querySelectorAll("[data-batch-del]").forEach((btn) => {
          btn.addEventListener("click", async () => {
            const id = btn.dataset.batchDel;
            const title = btn.dataset.batchTitle;
            if (!confirm(`删除批次「${title}」？\n批次任务及其关联的质检报告将一并删除，且不可恢复。`)) return;
            btn.disabled = true;
            try {
              await API.del(`/api/batch/${id}`);
              UI.toast("批次已删除", "success");
              loadHistory();
            } catch (err) {
              UI.handleError(err, "删除失败");
              btn.disabled = false;
            }
          });
        });
      })
      .catch((err) => UI.handleError(err, "批次历史加载失败"));
  }

  function renderImportResult(data) {
    const box = document.getElementById("batch-import-result");
    const st = data.source_stats || {};
    box.innerHTML = `
      ${statGrid([
        { label: "客户会话", value: st.customer_count ?? 0 },
        { label: "参与助理", value: st.assistant_count ?? 0 },
        { label: "消息条数", value: st.message_count ?? 0 },
        { label: "评分任务", value: st.task_count ?? 0 },
      ])}
      <div class="alert ${(data.warnings || []).length ? "alert-warning" : "alert-info"} mb-8 mt-16">
        <div>${(data.warnings || []).length ? "⚠️" : "ℹ️"}</div>
        <div>${(data.warnings || []).length
          ? data.warnings.map((w) => `<div>· ${UI.esc(w)}</div>`).join("")
          : "切分完成，每个客户会话将作为独立评分任务"}</div>
      </div>
      ${data.parse_error ? `<div class="alert alert-critical mb-8"><div>❌</div><div>解析失败：${UI.esc(data.parse_error)}（将生成 1 个失败任务，可修正格式后重新导入）</div></div>` : ""}
      <div class="card mt-16">
        <div class="card-title">客户会话列表（${(data.customers || []).length} 个）</div>
        <table class="table">
          <thead><tr><th>客户</th><th>消息数</th><th>参与助理</th></tr></thead>
          <tbody>
            ${(data.customers || []).map((c) => `
              <tr>
                <td><b>${UI.esc(c.customer_name)}</b></td>
                <td class="num">${c.message_count}</td>
                <td>${(c.assistant_names || []).map((n) => `<span class="tag">${UI.esc(n)}</span>`).join(" ") || '<span class="muted">-</span>'}</td>
              </tr>`).join("")}
          </tbody>
        </table>
      </div>
      <div class="form-actions mt-16">
        <button class="btn btn-primary" id="btn-batch-start">▶ 开始批量评分（${data.task_count} 个任务）</button>
        <span class="muted" style="font-size:12px">评分在后台自动进行，可随时关闭本页稍后回来查看</span>
      </div>`;
    box.querySelector("#btn-batch-start").addEventListener("click", () => {
      location.hash = `#/batch/${data.batch_id}`;
    });
  }

  // 当前待导入内容：{rooms} 房间模式（Excel 房间导出）或 null（走文本域 raw_text）
  let pendingRooms = null;

  async function renderImport() {
    clearPoll();
    pendingRooms = null;
    const app = document.getElementById("app");
    app.innerHTML = `
      <div class="card">
        <div class="card-title">批量评分历史</div>
        <div id="batch-history" class="mt-8"></div>
      </div>
      <div class="card mt-16">
        <div class="card-title">批量导入聊天记录</div>
        <div class="form-row" style="max-width:480px">
          <label class="form-label">批次标题（可选）</label>
          <input class="input" id="batch-title" placeholder="如：8月28日 批量质检" maxlength="200">
        </div>
        <div class="form-row">
          <label class="form-label">聊天记录（粘贴或上传，多个客户自动切分；单个客户超长会自动分段评分）</label>
          <textarea class="textarea" id="batch-raw" rows="12"
            placeholder="把多个客户的聊天记录一次性粘贴到这里…
支持：导出文本（客户昵称 + 时间 + 内容）、【客户】【助理A】标签文本、CSV/JSON、Excel(.xlsx/.xls)。
Excel 房间对话导出（含「完整聊天记录」列）自动按房间识别，每个房间一个独立评分任务。
不同客户昵称变化处 = 新会话。"></textarea>
        </div>
        <div class="form-actions">
          <button class="btn btn-ghost" id="btn-batch-file" type="button">📄 选择文件</button>
          <button class="btn btn-primary" id="btn-batch-import" type="button">开始导入</button>
          <input type="file" id="batch-file-input" accept=".txt,.csv,.json,.md,.xlsx,.xls" style="display:none">
        </div>
        <div id="batch-import-result" class="mt-16"></div>
      </div>`;
    loadHistory();
    const fileInput = app.querySelector("#batch-file-input");
    const rawEl = app.querySelector("#batch-raw");
    const importBtn = app.querySelector("#btn-batch-import");
    app.querySelector("#btn-batch-file").addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", async () => {
      const f = fileInput.files && fileInput.files[0];
      if (!f) return;
      try {
        const parsed = await readFile(f);
        if (parsed.rooms) {
          // 房间导出：每个房间一个独立客户会话；文本域锁定并显示摘要
          pendingRooms = parsed.rooms;
          rawEl.disabled = true;
          rawEl.value = `已识别为「Excel 房间对话导出」：${parsed.rooms.length} 个房间（客户会话），
每个房间将作为独立评分任务。如非房间导出文件，请重新选择文件。`;
          importBtn.textContent = `开始导入（${parsed.rooms.length} 个房间）`;
          UI.toast(`已识别 ${parsed.rooms.length} 个房间（完整聊天记录列）`, "info");
        } else {
          pendingRooms = null;
          rawEl.disabled = false;
          rawEl.value = parsed.text;
          importBtn.textContent = "开始导入";
          UI.toast("文件已载入，请确认后导入", "info");
        }
      } catch (err) {
        UI.handleError(err, "文件读取失败");
      }
      fileInput.value = "";
    });
    rawEl.addEventListener("input", () => {
      // 用户手动编辑文本域 → 视为普通文本导入
      if (rawEl.disabled) {
        rawEl.disabled = false;
        pendingRooms = null;
        importBtn.textContent = "开始导入";
      }
    });
    importBtn.addEventListener("click", async () => {
      const title = app.querySelector("#batch-title").value.trim() || null;
      const btn = importBtn;
      let payload;
      if (pendingRooms) {
        payload = { rooms: pendingRooms, title };
      } else {
        const raw = rawEl.value;
        if (!raw || !raw.trim()) {
          UI.toast("请先粘贴或上传聊天记录", "error");
          return;
        }
        payload = { raw_text: raw, title };
      }
      btn.disabled = true;
      try {
        const data = await API.post("/api/batch/import", payload);
        renderImportResult(data);
        loadHistory();
        UI.toast(`导入成功：${data.task_count} 个评分任务`, "success");
      } catch (err) {
        UI.handleError(err, "导入失败");
      } finally {
        btn.disabled = false;
      }
    });
  }

  // ---------- 进度页 ----------

  function renderProgressBar(stats) {
    return `
      <div class="progress" style="height:10px"><span style="width:${stats.percent}%"></span></div>
      <div class="stat-extra">已完成 ${stats.done}/${stats.total} · 处理中 ${stats.processing} · 重试中 ${stats.retrying} · 待处理 ${stats.pending} · 失败 ${stats.failed}</div>`;
  }

  function renderTaskRows(items) {
    return (items || []).map((t) => {
      // 每位助理各自一份报告：逐条渲染「姓名 分数」链接，点击分别进入对应报告
      const reps = t.reports || [];
      const scoreCell = reps.length
        ? `<span class="num">${reps.map((r) =>
            `<a style="color:var(--good);font-weight:600" href="#/report/${r.inspection_id}">${UI.esc(r.assistant_name)} ${r.total_score} 分</a>`
          ).join('<span class="muted"> · </span>')}</span>
          ${t.overview_id ? `<div class="mt-4"><a class="btn btn-ghost btn-sm" href="#/overview/${t.overview_id}">📊 查看本次总览（对比+优缺点）</a></div>` : ""}`
        : '<span class="muted">-</span>';
      const errCell = t.error
        ? `<span class="tag" title="${UI.esc(t.error)}">⚠ ${UI.esc(t.error.slice(0, 40))}${t.error.length > 40 ? "…" : ""}</span>`
        : '<span class="muted">-</span>';
      return `<tr>
        <td class="num muted">${UI.esc(t.task_id)}</td>
        <td><b>${UI.esc(t.customer_name)}</b> ${emotionBadge(t.current_emotion)}</td>
        <td>${(t.assistant_names || []).map((n) => `<span class="tag">${UI.esc(n)}</span>`).join(" ") || '<span class="muted">-</span>'}</td>
        <td class="num">${t.message_count}</td>
        <td>${statusBadge(t.status)}${t.retry_count ? `<span class="muted" style="font-size:11px">重试${t.retry_count}次</span>` : ""}</td>
        <td>${scoreCell}${t.degraded ? '<span class="muted" style="font-size:11px">（降级汇总）</span>' : ""}</td>
        <td>${errCell}</td>
        <td class="muted num">${UI.fmtDate(t.updated_at)}</td>
      </tr>`;
    }).join("");
  }

  function renderProgress(data) {
    const app = document.getElementById("app");
    const st = data.stats || {};
    const finished = data.status === "done";
    const allSucceeded = finished && !(st.failed || 0);
    const failedCount = st.failed || 0;
    app.innerHTML = `
      <div class="card">
        <div class="card-title" style="display:flex;align-items:center;gap:10px">
          <span>${UI.esc(data.title || data.batch_id.slice(0, 8))}</span>
          <span>${statusBadge(data.status)}</span>
          ${finished ? `<span class="badge ${allSucceeded ? "badge-good" : "badge-warning"}">${allSucceeded ? "全部完成" : "处理结束"}</span>` : ""}
        </div>
        <div class="mt-16">${statGrid([
          { label: "总任务", value: st.total ?? 0 },
          { label: "已完成", value: st.completed ?? 0 },
          { label: "处理中", value: (st.processing ?? 0) + (st.retrying ?? 0) },
          { label: "待处理", value: st.pending ?? 0 },
          { label: "失败", value: failedCount },
        ])}</div>
        <div class="mt-16">${renderProgressBar(st)}</div>
        <div class="form-actions mt-16" style="display:${failedCount ? "flex" : "none"};gap:10px">
          <button class="btn btn-danger" id="btn-batch-retry">↻ 重新评分失败任务（${failedCount}）</button>
        </div>
      </div>
      <div class="card mt-16">
        <div class="card-title">任务列表</div>
        <table class="table">
          <thead><tr><th>任务</th><th>客户</th><th>参与助理</th><th>消息数</th><th>状态</th><th>评分</th><th>错误</th><th>更新时间</th></tr></thead>
          <tbody>${renderTaskRows(data.items)}</tbody>
        </table>
      </div>`;
    const retryBtn = app.querySelector("#btn-batch-retry");
    if (retryBtn) {
      retryBtn.addEventListener("click", async () => {
        retryBtn.disabled = true;
        try {
          const r = await API.post(`/api/batch/${data.batch_id}/retry-failed`, {});
          UI.toast(`已重新排队 ${r.reset_count} 个失败任务`, "success");
          startPolling(data.batch_id);
        } catch (err) {
          UI.handleError(err, "重试失败");
          retryBtn.disabled = false;
        }
      });
    }
  }

  function startPolling(batchId) {
    clearPoll();
    pollTimer = setInterval(() => {
      API.get(`/api/batch/${batchId}/progress`)
        .then((data) => {
          renderProgress(data);
          if (data.status === "done" && (data.stats || {}).pending === 0) {
            clearPoll();
            UI.toast((data.stats || {}).failed ? "批量评分已结束，存在失败任务" : "批量评分全部完成", (data.stats || {}).failed ? "info" : "success");
          }
        })
        .catch((err) => {
          clearPoll();
          UI.handleError(err, "进度刷新失败");
        });
    }, POLL_MS);
  }

  async function renderProgressPage(batchId) {
    clearPoll();
    const app = document.getElementById("app");
    app.innerHTML = `<div class="card"><div class="card-title">批量评分</div><div class="spinner" style="margin:24px auto"></div></div>`;
    try {
      const data = await API.get(`/api/batch/${batchId}/progress`);
      renderProgress(data);
      // 启动时自动开始评分（幂等）；未完成则轮询
      const started = await API.post(`/api/batch/${batchId}/start`, {});
      if (started && started.started) UI.toast("开始批量评分", "success");
      if (data.status !== "done") startPolling(batchId);
    } catch (err) {
      app.innerHTML = "";
      UI.handleError(err, "批次加载失败");
      setTimeout(() => { location.hash = "#/batch"; }, 1200);
    }
  }

  window.Views.batch = {
    render: () => renderImport(),
    renderProgress: (batchId) => renderProgressPage(batchId),
  };
})();
