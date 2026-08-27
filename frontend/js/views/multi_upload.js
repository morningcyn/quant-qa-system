// 多人质检页：粘贴完整聊天 → 预览（自动识别各助理）→ 归属员工确认 → 批量质检 + 总览
(function () {
  "use strict";
  window.Views = window.Views || {};

  const STAGES = [
    "正在解析多人对话…",
    "正在并发评分 N 位助理…",
    "正在生成本次客户服务总览…",
  ];

  function readFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(new Error("文件读取失败"));
      reader.readAsText(file, "utf-8");
    });
  }

  function buildStageBox(onCancel) {
    const box = document.createElement("div");
    box.className = "stage-box";
    box.innerHTML = `
      <div class="stage-title">多人质检进行中，请稍候…</div>
      <div class="stage-steps">
        ${STAGES.map((s) => `<div class="stage-step"><span class="dot"></span><span>${s}</span></div>`).join("")}
      </div>
      <div class="mt-16"><button class="btn btn-ghost" id="btn-cancel-run">取消</button></div>`;
    box.querySelector("#btn-cancel-run").addEventListener("click", () => {
      if (onCancel) onCancel();
    });
    return box;
  }

  function runStages(box) {
    const steps = box.querySelectorAll(".stage-step");
    let i = 0;
    const timer = setInterval(() => {
      if (i < steps.length) steps[i].classList.add("active");
      if (i > 0) steps[i - 1].classList.replace("active", "done");
      i++;
      if (i > steps.length) clearInterval(timer);
    }, 7000);
    return timer;
  }

  function roleBadge(role) {
    return role === "客"
      ? `<span class="badge badge-neutral">客</span>`
      : `<span class="badge badge-blue">助</span>`;
  }

  function alertHtml(data) {
    const warns = data.warnings || [];
    return `
      <div class="alert ${warns.length ? "alert-warning" : "alert-info"} mb-8">
        <div>${warns.length ? "⚠️" : "ℹ️"}</div>
        <div>${warns.length ? warns.map((w) => `<div>· ${UI.esc(w)}</div>`).join("") : "格式识别正常，可以开始质检"}
        <div style="margin-top:4px;color:var(--text-2)">共识别 ${data.role_stats.total} 轮（客户 ${data.role_stats["客"] || 0} 轮 / 助理 ${data.role_stats["助"] || 0} 轮）· 格式：${UI.esc(data.fmt)}</div></div>
      </div>`;
  }

  function messageStreamHtml(data) {
    const msgs = data.messages || [];
    const shown = msgs.slice(0, 120);
    return `
      <div class="card mt-16">
        <div class="card-title">消息流（可追溯原始记录）</div>
        <div style="max-height:340px;overflow-y:auto">
          ${shown.map((m) => `
            <div class="turn-bubble ${m.role === "客" ? "customer" : "assistant"}">
              <span class="turn-no num">#${m.turn_no}</span>
              <span class="muted" style="margin-left:4px">${m.timestamp ? UI.esc(m.timestamp) : ""}</span>
              <span style="margin-left:6px">${roleBadge(m.role)}</span>
              <div class="bubble">
                <b>${UI.esc(m.speaker)}</b>${m.canonical_name && m.canonical_name !== m.speaker ? `<span class="muted">（归并：${UI.esc(m.canonical_name)}）</span>` : ""}
                ：${m.text.length > 500
                  ? `<details style="display:inline"><summary>${UI.esc(m.text.slice(0, 500))}…（展开全文）</summary><div style="white-space:pre-wrap;display:inline">${UI.esc(m.text)}</div></details>`
                  : UI.esc(m.text)}
              </div>
            </div>`).join("")}
          ${msgs.length > shown.length ? `<div class="muted" style="text-align:center">… 仅预览前 120 轮</div>` : ""}
        </div>
      </div>`;
  }

  // 归属员工下拉（默认选中自动匹配结果；未匹配默认"请选择员工…"）。
  // 唯一事实源 = 页面 DOM：canonical_name 记在 select 的 data 属性上，
  // 提交时直接从 DOM 收集映射，不依赖任何计数闭包。
  function buildAssistantRow(a, employees, onMappingChange) {
    const row = document.createElement("tr");
    const matched = a.matched_assistant_id;
    const options = [
      `<option value="">${matched ? "" : "请选择员工…"}</option>`,
      ...employees.map((e) => {
        const sel = e.id === matched ? "selected" : "";
        return `<option value="${e.id}" ${sel}>${UI.esc(e.name)}（${UI.esc(e.employee_no)}）</option>`;
      }),
    ].join("");
    row.innerHTML = `
      <td><b>${UI.esc(a.display_name)}</b></td>
      <td class="muted" style="font-size:12px">${(a.aliases || []).map((x) => UI.esc(x)).join("、") || "-"}</td>
      <td class="num">${a.reply_count} 次</td>
      <td class="num">${UI.esc(a.turn_range || "-")}</td>
      <td>
        <div style="display:flex;gap:6px;align-items:center">
          <select class="select m-employee" style="min-width:170px" data-canonical="${UI.esc(a.canonical_name)}">${options}</select>
          <span class="m-pending" style="color:var(--critical);font-size:12px;display:${matched != null ? "none" : ""}">未确认归属</span>
          <button class="btn btn-ghost m-create" type="button">新建员工</button>
        </div>
      </td>`;
    const sel = row.querySelector(".m-employee");
    sel.value = matched != null ? String(matched) : "";
    sel.addEventListener("change", () => {
      const tip = row.querySelector(".m-pending");
      if (tip) tip.style.display = sel.value ? "none" : "";
      onMappingChange();
    });
    row.querySelector(".m-create").addEventListener("click", () => {
      createEmployeeInline(row, employees, sel, onMappingChange);
    });
    return row;
  }

  // 内联"新建员工"表单：姓名/工号/模板 → POST /api/assistants → 刷新下拉并选中
  async function createEmployeeInline(row, employees, sel, onMappingChange) {
    const cell = sel.closest("td");
    const wrap = cell.querySelector(".m-create").parentElement;
    const form = document.createElement("div");
    form.className = "m-create-form";
    form.innerHTML = `
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
        <input class="input m-name" placeholder="姓名" style="width:110px">
        <input class="input m-no" placeholder="工号" style="width:110px">
        <select class="select m-tpl" style="width:150px">
          <option value="standard">标准服务模板</option>
          <option value="newbie">新人辅导模板</option>
          <option value="vip">高净值客户模板</option>
        </select>
        <button class="btn btn-primary m-save" type="button">保存</button>
        <button class="btn btn-ghost m-close" type="button">取消</button>
      </div>`;
    wrap.appendChild(form);
    form.querySelector(".m-name").focus();
    const cleanup = () => form.remove();
    form.querySelector(".m-close").addEventListener("click", cleanup);
    form.querySelector(".m-save").addEventListener("click", async () => {
      const name = form.querySelector(".m-name").value.trim();
      const no = form.querySelector(".m-no").value.trim();
      if (!name || !no) { UI.toast("请填写姓名和工号", "warning"); return; }
      const saveBtn = form.querySelector(".m-save");
      saveBtn.disabled = true;
      try {
        const emp = await API.post("/api/assistants", {
          name,
          employee_no: no,
          template_type: form.querySelector(".m-tpl").value,
        });
        employees.push(emp);
        const opt = document.createElement("option");
        opt.value = String(emp.id);
        opt.textContent = `${emp.name}（${emp.employee_no}）`;
        sel.appendChild(opt);
        sel.value = String(emp.id);
        sel.dispatchEvent(new Event("change"));
        cleanup();
        UI.toast(`已新建员工：${emp.name}`, "success");
      } catch (err) {
        UI.handleError(err, "新建员工失败");
        saveBtn.disabled = false;
      }
    });
  }

  // 历史总览列表：分页加载最近质检对比，点击行跳转总览页（结果定格留存、随时回看）
  async function loadHistory(app) {
    const body = app.querySelector("#m-history-body");
    const count = app.querySelector("#m-history-count");
    let page = 1;
    const loadPage = async (append) => {
      try {
        const data = await API.get(`/api/overviews?page=${page}&page_size=10`);
        if (count) count.textContent = data.total ? `· 共 ${data.total} 次质检` : "";
        const rows = (data.items || []).map((o) => `
          <a href="#/overview/${o.id}" style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--axis);text-decoration:none;color:var(--text)">
            <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${UI.esc(o.title)}</span>
            <span class="badge badge-neutral">${o.participant_count} 人</span>
            ${o.degraded ? `<span class="badge badge-warning">规则生成</span>` : ""}
            <span class="muted" style="font-size:12px;flex-shrink:0">${UI.fmtDate(o.created_at)}</span>
          </a>`).join("");
        if (append) {
          body.querySelector("#m-more")?.remove();
          body.insertAdjacentHTML("beforeend", rows);
        } else {
          body.innerHTML = rows || `<div class="muted">暂无历史质检记录，完成一次多人质检后这里可随时回看对比结果</div>`;
        }
        if (page * 10 < data.total) {
          const more = document.createElement("button");
          more.id = "m-more";
          more.className = "btn btn-ghost";
          more.textContent = `加载更多（还有 ${data.total - page * 10} 条）`;
          more.addEventListener("click", () => { page += 1; loadPage(true); });
          body.appendChild(more);
        }
      } catch (err) {
        if (!append) body.innerHTML = `<div class="muted">历史列表加载失败</div>`;
      }
    };
    loadPage(false);
  }

  Views.multiUpload = {
    render() {
      const app = document.getElementById("app");
      let preview = null; // 当前预览数据
      let employees = [];
      app.innerHTML = `
        <div class="page-head">
          <div>
            <div class="page-title">多人质检</div>
            <div class="page-sub">一个客户的完整聊天可能由多位助理共同完成：粘贴完整聊天 → 系统按确定性规则自动识别各助理 → 并发生成各自的质检报告与本次客户服务总览</div>
          </div>
        </div>

        <div class="card mb-16" id="m-history">
          <div class="card-title">历史总览<span class="muted" style="font-size:12px;font-weight:400;margin-left:8px" id="m-history-count"></span></div>
          <div id="m-history-body"><div class="muted">正在加载…</div></div>
        </div>

        <div class="card">
          <div class="card-title">① 输入完整聊天记录</div>
          <div class="form-row">
            <label class="form-label">会话标题（可选）</label>
            <input class="input" id="m-title" maxlength="200" placeholder="例如：宇树客户服务（2026-08-24）">
          </div>
          <div class="tabs" id="m-tabs">
            <span class="tab active" data-tab="paste">粘贴文本</span>
            <span class="tab" data-tab="file">上传文件</span>
          </div>
          <div id="m-pane-paste">
            <textarea class="textarea" id="m-text" style="min-height:200px" placeholder="粘贴客户的完整聊天记录（多助理共同服务的完整过程），支持格式：
客户 哈尔滨赢家1122 2026-05-29 15:30:14
您在机构圈子里解读的股票我天天看，现在跌了不少。
助理韩珂龙头班（王萌）
2026-05-29 17:32:56
不用慌，我帮您看看这只票的结构。

也支持 [客]…/[助]…、[王萌] 内容、CSV（时间,角色,内容）、JSON"></textarea>
          </div>
          <div id="m-pane-file" style="display:none">
            <input type="file" id="m-file" accept=".txt,.csv,.json" style="color:var(--text-2)">
            <div class="form-hint">支持 TXT / CSV / JSON 导出文件（UTF-8 编码）</div>
          </div>
          <div id="m-organize-bar" style="display:none" class="mt-16"></div>
          <div id="m-preview" style="display:none" class="mt-16"></div>
          <div class="form-actions">
            <button class="btn btn-ghost" id="m-reset">清空</button>
            <button class="btn btn-ghost" id="m-organize-btn">🪄 自动识别并整理</button>
            <button class="btn btn-ghost" id="m-preview-btn">② 解析预览</button>
            <button class="btn btn-primary" id="m-submit" disabled>③ 一键批量质检并生成总览</button>
          </div>
        </div>`;

      // 历史总览列表（异步填充，不阻塞质检主流程；失败静默降级）
      loadHistory(app);

      // tab 切换
      app.querySelectorAll("#m-tabs .tab").forEach((tab) => {
        tab.addEventListener("click", () => {
          app.querySelectorAll("#m-tabs .tab").forEach((t) => t.classList.remove("active"));
          tab.classList.add("active");
          const isPaste = tab.dataset.tab === "paste";
          app.querySelector("#m-pane-paste").style.display = isPaste ? "" : "none";
          app.querySelector("#m-pane-file").style.display = isPaste ? "none" : "";
        });
      });
      app.querySelector("#m-file").addEventListener("change", async () => {
        const file = app.querySelector("#m-file").files[0];
        if (!file) return;
        try {
          const text = await readFile(file);
          app.querySelector("#m-text").value = text;
          UI.toast(`已读取文件：${file.name}（${text.length} 字）`, "success");
        } catch (err) {
          UI.handleError(err, "文件读取失败");
        }
      });
      app.querySelector("#m-reset").addEventListener("click", () => {
        app.querySelector("#m-text").value = "";
        clearPreview();
        hideOrganizeBar();
        UI.toast("已清空", "info");
      });

      // 🪄 自动识别并整理：粘贴原始记录 → 后端生成【客户】【助理A】标签文本 → 回填 textarea。
      // 原始文本备份到 localStorage，整理结果条提供"恢复原文"（完整原始记录可追溯）。
      const ORGANIZE_BACKUP_KEY = "multi_organize_backup";
      async function doOrganize() {
        const ta = app.querySelector("#m-text");
        const text = ta.value.trim();
        if (!text) { UI.toast("请先粘贴或上传完整聊天记录", "warning"); return; }
        const btn = app.querySelector("#m-organize-btn");
        btn.disabled = true;
        try {
          const data = await API.post("/api/parse/organize", { raw_text: text });
          localStorage.setItem(ORGANIZE_BACKUP_KEY, text);
          ta.value = data.organized_text;
          clearPreview();
          showOrganizeBar(data);
          UI.toast(
            `已自动识别并整理 ${data.message_count} 条消息（客户 ${data.role_stats["客"] || 0} 条 / 助理 ${data.role_stats["助"] || 0} 条 · 识别到 ${(data.assistants || []).length} 位助理）`,
            "success"
          );
        } catch (err) {
          UI.handleError(err, "自动识别失败");
        } finally {
          btn.disabled = false;
        }
      }

      function showOrganizeBar(data) {
        const bar = app.querySelector("#m-organize-bar");
        const assistants = (data.assistants || [])
          .map((a) => `${UI.esc(a.label)}→${UI.esc(a.canonical_name)}`)
          .join("、");
        bar.style.display = "";
        bar.innerHTML = `
          <div class="card">
            <div class="card-title">🪄 已自动识别并整理（${data.message_count} 条消息 · 识别到 ${(data.assistants || []).length} 位助理）</div>
            <div class="form-hint">
              已为每条消息添加【客户】/【助理X】标签（标签对应：${assistants || "—"}）。
              若多条【助理X】消息实际由<b>不同助理</b>回复（同名显示名无法自动区分）：直接把部分标签改成<b>助理真实姓名</b>，
              如把【助理A】改成【段勇亮】、【徐艺桐】，再点「② 解析预览」——系统会自动匹配员工、分别归属。
              原始记录已备份，可随时
              <button class="btn btn-ghost btn-sm" id="m-organize-restore">恢复原文</button>
            </div>
          </div>`;
        bar.querySelector("#m-organize-restore").addEventListener("click", () => {
          const backup = localStorage.getItem(ORGANIZE_BACKUP_KEY);
          if (backup === null) return;
          app.querySelector("#m-text").value = backup;
          clearPreview();
          hideOrganizeBar();
          UI.toast("已恢复原始记录", "info");
        });
      }

      function hideOrganizeBar() {
        const bar = app.querySelector("#m-organize-bar");
        bar.style.display = "none";
        bar.innerHTML = "";
      }

      function clearPreview() {
        const box = app.querySelector("#m-preview");
        box.style.display = "none";
        box.innerHTML = "";
        preview = null;
        refreshSubmit();
      }

      // 每次变更后按页面 DOM 里已确认归属的助理数重算（唯一事实源，不做增减计数）
      function refreshSubmit() {
        const btn = app.querySelector("#m-submit");
        if (!btn) return;
        const total = preview ? preview.assistants.length : 0;
        const sels = app.querySelectorAll(".m-employee");
        const done = sels.length ? Array.from(sels).filter((s) => s.value).length : 0;
        const all = total > 0 && done === total;
        btn.disabled = !all;
        btn.title = all ? "" : total === 0
          ? "未识别到助理消息，请检查聊天记录格式"
          : `还有 ${total - done} 位助理未确认归属员工`;
      }

      // 从页面 DOM 收集归属映射（canonical_name → 员工 id）
      function collectMapping() {
        const mapping = {};
        app.querySelectorAll(".m-employee").forEach((s) => {
          if (s.value) mapping[s.dataset.canonical] = Number(s.value);
        });
        return mapping;
      }

      async function doPreview() {
        const text = app.querySelector("#m-text").value.trim();
        if (!text) { UI.toast("请先粘贴或上传完整聊天记录", "warning"); return; }
        const btn = app.querySelector("#m-preview-btn");
        btn.disabled = true;
        try {
          const data = await API.post("/api/parse/preview-multi", { raw_text: text });
          preview = data;
          employees = (await API.get("/api/assistants")).assistants || [];
          const box = app.querySelector("#m-preview");
          const rows = (data.assistants || []).map((a) =>
            buildAssistantRow(a, employees, refreshSubmit)
          ).map((tr) => tr.outerHTML).join("");

          box.style.display = "";
          box.innerHTML = `
            ${alertHtml(data)}
            ${(!data.assistants || data.assistants.length === 0)
              ? `<div class="alert alert-warning mb-8"><div>⚠️</div><div>未识别到助理消息（无法从记录中识别出助侧发言），请检查聊天记录格式后重新解析。已识别到的客户轮会保留在下方消息流中供排查。</div></div>`
              : ""}
            <div style="display:flex;gap:16px;flex-wrap:wrap">
              <div class="stat-tile"><div class="stat-value num">${data.role_stats.total}</div><div class="stat-label">总轮次</div></div>
              <div class="stat-tile"><div class="stat-value num">${data.role_stats["客"] || 0}</div><div class="stat-label">客户轮</div></div>
              <div class="stat-tile"><div class="stat-value num">${(data.assistants || []).length}</div><div class="stat-label">识别助理</div></div>
            </div>
            ${data.assistants && data.assistants.length ? `
              <div class="card mt-16">
                <div class="card-title">识别到的助理（确认每位助理的归属员工）</div>
                <table class="table">
                  <thead><tr><th>助理</th><th>别名/原始标识</th><th>回复数</th><th>回复轮次</th><th>归属员工</th></tr></thead>
                  <tbody>${rows}</tbody>
                </table>
                <div class="form-hint">未自动匹配的助理请手动选择归属员工，或点击「新建员工」录入。全部确认后才能开始质检。</div>
              </div>` : ""}
            ${messageStreamHtml(data)}`;
          refreshSubmit();
        } catch (err) {
          UI.handleError(err, "解析失败");
        } finally {
          btn.disabled = false;
        }
      }

      app.querySelector("#m-organize-btn").addEventListener("click", doOrganize);
      app.querySelector("#m-preview-btn").addEventListener("click", doPreview);
      app.querySelector("#m-text").addEventListener("input", () => { if (preview) clearPreview(); hideOrganizeBar(); });

      app.querySelector("#m-submit").addEventListener("click", async () => {
        const raw = app.querySelector("#m-text").value.trim();
        const title = app.querySelector("#m-title").value.trim();
        const submitBtn = app.querySelector("#m-submit");
        const n = preview.assistants.length;

        // 全屏遮罩接管视觉（不动原卡片：失败/取消时移除遮罩即可原样重试，
        // 不再销毁预览导致页面永久停在"进行中"）
        const overlay = document.createElement("div");
        overlay.className = "stage-overlay";
        const stageBox = buildStageBox(() => {
          overlay.remove();
          submitBtn.disabled = false;
          UI.toast("已取消等待（已完成的报告仍会生成）", "warning");
        });
        overlay.appendChild(stageBox);
        document.body.appendChild(overlay);
        runStages(stageBox);
        submitBtn.disabled = true;

        try {
          const data = await API.post("/api/inspections/batch", {
            raw_dialogue: raw,
            session_title: title || null,
            mapping: collectMapping(),
          }, 200000 + n * 60000);

          // 请求已结束：恢复页面（结果通过 toast + 跳转呈现，失败详情见错误块）
          overlay.remove();
          submitBtn.disabled = false;

          // 渲染结果（错误明细展示在预览卡片下方）
          if (data.errors && data.errors.length) {
            const errCard = document.createElement("div");
            errCard.className = "card mt-16";
            errCard.innerHTML = `
              <div class="card-title">质检结果</div>
              ${data.errors.map((e) => `
                <div class="alert alert-critical mb-8"><div>✖</div><div><b>${UI.esc(e.display_name)}（${UI.esc(e.canonical_name)}）</b>：${UI.esc(e.message)}</div></div>`).join("")}
              ${data.reports.length ? `<div class="alert alert-info"><div>ℹ️</div><div>其余 ${data.reports.length} 位助理报告已生成，正在打开总览…</div></div>` : `<div class="alert alert-warning"><div>⚠️</div><div>全部助理质检失败，请检查模型配置后重试。</div></div>`}`;
            app.querySelector("#m-preview").insertAdjacentElement("afterend", errCard);
            setTimeout(() => errCard.remove(), 15000);
          }
          if (data.reports.length) {
            UI.toast(`质检完成：${data.reports.length} 份报告已归档${data.errors.length ? `，${data.errors.length} 位助理失败` : ""}`, data.errors.length ? "warning" : "success");
            location.hash = `#/overview/${data.overview_id}`;
          } else {
            UI.toast("全部助理质检失败，请重试", "error");
          }
        } catch (err) {
          // 关键修复：失败也必须恢复页面，不能停留在"多人质检进行中"
          overlay.remove();
          submitBtn.disabled = false;
          if (err.code === "not_configured") {
            UI.toast(err.message, "error");
            location.hash = "#/settings";
            return;
          }
          UI.handleError(err, "批量质检失败，请重试（输入与归属确认均已保留）");
        }
      });
    },
  };
})();
