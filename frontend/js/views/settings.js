// 设置页：我的模型（API Key 配置）/ 质检模板 / 关于
(function () {
  "use strict";
  window.Views = window.Views || {};

  const PROTOCOL_NAMES = { openai_compat: "OpenAI 兼容", anthropic: "Anthropic" };

  // ---------- 我的模型 ----------

  function statusDot(model) {
    const st = model.last_test_status;
    if (st === "ok") return `<span class="status-dot ok" title="连通正常"></span>`;
    if (st === "failed") return `<span class="status-dot failed" title="连通失败"></span>`;
    return `<span class="status-dot unknown" title="未测试"></span>`;
  }

  function openModelForm(meta, existing) {
    const isEdit = !!existing;
    const form = document.createElement("div");
    const presets = (meta && meta.preset_base_urls) || { openai_compat: "https://api.deepseek.com/v1", anthropic: "https://api.anthropic.com" };
    const suggestions = (meta && meta.model_suggestions) || {};
    form.innerHTML = `
      <div class="form-row">
        <label class="form-label">协议</label>
        <select class="select" id="m-protocol">
          <option value="openai_compat" ${!existing || existing.protocol === "openai_compat" ? "selected" : ""}>OpenAI 兼容（DeepSeek / GLM / Qwen / GPT）</option>
          <option value="anthropic" ${existing && existing.protocol === "anthropic" ? "selected" : ""}>Anthropic（Claude）</option>
        </select>
      </div>
      <div class="form-row">
        <label class="form-label">名称</label>
        <input class="input" id="m-name" maxlength="50" value="${existing ? UI.esc(existing.name) : ""}" placeholder="例如：DeepSeek 主用">
      </div>
      <div class="form-row">
        <label class="form-label">API 地址（base_url）</label>
        <input class="input" id="m-base" value="${existing ? UI.esc(existing.base_url) : ""}" placeholder="https://api.deepseek.com/v1">
      </div>
      <div class="form-row">
        <label class="form-label">模型名称</label>
        <input class="input" id="m-model" list="m-model-list" value="${existing ? UI.esc(existing.model_name) : ""}" placeholder="deepseek-chat">
        <datalist id="m-model-list">
          ${(suggestions[existing ? existing.protocol : "openai_compat"] || []).map((x) => `<option value="${x}">`).join("")}
        </datalist>
      </div>
      <div class="form-row">
        <label class="form-label">API Key${isEdit ? "（留空 = 不修改）" : ""}</label>
        <input class="input" id="m-key" type="password" placeholder="${isEdit ? "••••••••（已保存，留空则不修改）" : "sk-…"}">
        <div class="form-hint">🔒 密钥仅保存在本机（Windows 加密存储），绝不上传</div>
      </div>
      <div class="form-row">
        <label class="form-label">温度（temperature）：<span class="muted" id="m-temp-val">${existing ? (existing.temperature || 0.2) : 0.2}</span></label>
        <input type="range" id="m-temp" min="0" max="1" step="0.1" value="${existing ? (existing.temperature || 0.2) : 0.2}" style="width:100%">
      </div>
      <div class="form-actions">
        <button class="btn btn-ghost" id="m-cancel">取消</button>
        <button class="btn btn-primary" id="m-save">保存</button>
      </div>`;
    const modal = UI.openModal({ title: isEdit ? "编辑模型" : "添加模型", content: form });
    const protocolSel = form.querySelector("#m-protocol");
    protocolSel.addEventListener("change", () => {
      if (!form.querySelector("#m-base").value || (!isEdit && protocolSel.value !== existing?.protocol)) {
        form.querySelector("#m-base").value = presets[protocolSel.value] || "";
      }
      const list = form.querySelector("#m-model-list");
      list.innerHTML = (suggestions[protocolSel.value] || []).map((x) => `<option value="${x}">`).join("");
    });
    if (!existing) form.querySelector("#m-base").value = presets["openai_compat"];
    form.querySelector("#m-temp").addEventListener("input", (e) => {
      form.querySelector("#m-temp-val").textContent = e.target.value;
    });
    modal.body.querySelector("#m-cancel").addEventListener("click", modal.close);
    modal.body.querySelector("#m-save").addEventListener("click", async () => {
      const payload = {
        id: existing ? existing.id : null,
        name: form.querySelector("#m-name").value.trim(),
        protocol: protocolSel.value,
        base_url: form.querySelector("#m-base").value.trim(),
        api_key: form.querySelector("#m-key").value.trim(),
        model_name: form.querySelector("#m-model").value.trim(),
        temperature: Number(form.querySelector("#m-temp").value),
      };
      try {
        await API.post("/api/settings/models", payload);
        modal.close();
        UI.toast("已保存", "success");
        refreshModelsTab();
      } catch (err) {
        UI.handleError(err, "保存失败");
      }
    });
  }

  async function refreshModelsTab() {
    const body = document.querySelector("#tab-models");
    if (!body) return;
    try {
      const data = await API.get("/api/settings/models");
      renderModels(body, data);
    } catch (err) {
      UI.handleError(err, "加载失败");
    }
  }

  function renderModels(body, data) {
    const models = data.models || [];
    body.innerHTML = `
      <div class="alert alert-info mb-16">
        <div>🔒</div>
        <div>API Key 仅保存在本机数据库中（Windows 加密存储），不会上传到任何服务器。请使用<b>您自己的</b>模型 Key。</div>
      </div>
      ${models.length ? "" : `<div class="empty" style="padding:24px">尚未配置模型，点击「添加模型」填写您的 API Key 后即可开始质检</div>`}
      ${models.map((m) => `
        <div class="model-card">
          <div class="mc-head">
            ${statusDot(m)}
            <b>${UI.esc(m.name || "未命名模型")}</b>
            <span class="badge badge-neutral">${PROTOCOL_NAMES[m.protocol] || m.protocol}</span>
            ${m.is_active ? `<span class="badge badge-blue">✔ 当前默认</span>` : ""}
            <div class="mc-actions">
              <button class="btn btn-sm btn-ghost" data-test="${m.id}">测试连通</button>
              ${!m.is_active ? `<button class="btn btn-sm btn-ghost" data-activate="${m.id}">设为默认</button>` : ""}
              <button class="btn btn-sm btn-ghost" data-edit="${m.id}">编辑</button>
              <button class="btn btn-sm btn-danger" data-del="${m.id}">删除</button>
            </div>
          </div>
          <div class="mt-8 muted" style="font-size:12px">
            ${UI.esc(m.base_url || "")} · 模型 <b>${UI.esc(m.model_name || "")}</b> · 温度 ${m.temperature ?? 0.2}
            ${m.has_api_key ? ` · <span style="color:var(--s-teal)">密钥已保存（仅本机可解密）</span>` : ` · <span style="color:var(--warning)">未填写 Key</span>`}
            ${m.last_test_at ? ` · 上次测试：${UI.fmtDate(m.last_test_at)}` : ""}
          </div>
          <div id="test-result-${m.id}"></div>
        </div>`).join("")}
      <div class="mt-16"><button class="btn btn-primary" id="btn-add-model">＋ 添加模型</button></div>`;

    body.querySelector("#btn-add-model").addEventListener("click", () => openModelForm(data, null));
    body.querySelectorAll("[data-edit]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const m = models.find((x) => String(x.id) === btn.dataset.edit);
        openModelForm(data, m);
      });
    });
    body.querySelectorAll("[data-activate]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await API.put(`/api/settings/models/${btn.dataset.activate}/activate`);
          UI.toast("已切换默认模型", "success");
          refreshModelsTab();
        } catch (err) { UI.handleError(err); }
      });
    });
    body.querySelectorAll("[data-test]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const slot = document.getElementById(`test-result-${btn.dataset.test}`);
        slot.innerHTML = `<div class="mt-8 muted"><span class="spinner" style="width:12px;height:12px;border-width:1.5px"></span> 正在测试…</div>`;
        btn.disabled = true;
        try {
          const r = await API.post(`/api/settings/models/${btn.dataset.test}/test`, undefined, 25000);
          slot.innerHTML = `<div class="mt-8 alert ${r.ok ? "alert-info" : "alert-warning"}">
            <div>${r.ok ? "✔" : "✗"}</div>
            <div>${UI.esc(r.message)}${r.latency_ms ? `（${r.latency_ms} ms）` : ""}</div></div>`;
          if (r.ok) UI.toast("连接成功", "success");
        } catch (err) {
          slot.innerHTML = `<div class="mt-8 alert alert-warning"><div>✗</div><div>${UI.esc((err && err.message) || "测试失败")}</div></div>`;
        } finally {
          btn.disabled = false;
          setTimeout(refreshModelsTab, 800);
        }
      });
    });
    body.querySelectorAll("[data-del]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const m = models.find((x) => String(x.id) === btn.dataset.del);
        if (!confirm(`确定删除模型配置「${m.name}」？`)) return;
        try {
          await API.del(`/api/settings/models/${m.id}`);
          UI.toast("已删除", "success");
          refreshModelsTab();
        } catch (err) { UI.handleError(err); }
      });
    });
  }

  // ---------- 质检模板 ----------

  function numberField(label, value, key) {
    return `<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
      <span style="flex:1;font-size:12px;color:var(--text-2)">${label}</span>
      <input class="input num" data-field="${key}" type="number" min="0" max="60" value="${value}" style="width:70px;padding:4px 8px">
    </div>`;
  }

  async function refreshTemplatesTab() {
    const body = document.querySelector("#tab-templates");
    if (!body) return;
    try {
      const data = await API.get("/api/settings/templates");
      renderTemplates(body, data);
    } catch (err) {
      UI.handleError(err, "加载失败");
    }
  }

  function renderTemplates(body, data) {
    const templates = data.templates || [];
    body.innerHTML = templates.map((t) => {
      const cfg = t.config || {};
      const d = cfg.d || {};
      const s = cfg.s || {};
      const dSum = Object.values(d).reduce((a, b) => a + (b.max || 0), 0);
      const sSum = Object.values(s).reduce((a, b) => a + (b.max || 0), 0);
      const dimFields = Object.keys(d).map((k) => numberField(`${k.toUpperCase()} ${d[k].name}`, d[k].max, `d.${k}.max`)).join("");
      const sFields = Object.keys(s).map((k) => {
        const subs = Object.keys(s[k].sub_items || {}).map((sk) =>
          numberField(`　└ ${s[k].sub_items[sk].name}`, s[k].sub_items[sk].max, `s.${k}.sub_items.${sk}.max`)
        ).join("");
        return numberField(`${k.toUpperCase()} ${s[k].name}`, s[k].max, `s.${k}.max`) + subs;
      }).join("");
      return `
      <div class="card" style="margin-bottom:14px" data-tpl="${t.template_type}">
        <div class="dim-head mb-8">
          <span class="dim-name">${UI.esc(t.name)}</span>
          <span class="muted" style="font-size:12px">D端合计 <b class="num">${dSum}</b>/55 · S端合计 <b class="num">${sSum}</b>/45 · 黄灯阈值 <b class="num">${cfg.yellow_threshold ?? 59}</b></span>
        </div>
        <div class="grid-2">
          <div>
            <div class="card-title">D 端权重（合计须 = 55）</div>
            ${dimFields}
          </div>
          <div>
            <div class="card-title">S 端权重（合计须 = 45）</div>
            ${sFields}
          </div>
        </div>
        <div class="mt-8" style="display:flex;gap:8px;justify-content:flex-end;align-items:center">
          <span style="margin-right:auto;font-size:12px" class="muted">黄灯阈值（低于该分触发预警）：
            <input class="input num" data-field="yellow_threshold" type="number" min="0" max="100" value="${cfg.yellow_threshold ?? 59}" style="width:64px;padding:4px 8px">
          </span>
          <button class="btn btn-sm btn-ghost" data-reset="${t.template_type}">恢复默认</button>
          <button class="btn btn-sm btn-primary" data-save="${t.template_type}">保存模板</button>
        </div>
      </div>`;
    }).join("");

    body.querySelectorAll("[data-save]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const card = btn.closest(".card");
        const tpl = templates.find((x) => x.template_type === card.dataset.tpl);
        const cfg = JSON.parse(JSON.stringify(tpl.config));
        card.querySelectorAll("[data-field]").forEach((inp) => {
          const path = inp.dataset.field.split(".");
          let node = cfg;
          for (let i = 0; i < path.length - 1; i++) node = node[path[i]];
          node[path[path.length - 1]] = Number(inp.value);
        });
        try {
          await API.put(`/api/settings/templates/${tpl.template_type}`, { name: tpl.name, config: cfg });
          UI.toast("模板已保存，仅影响之后的质检", "success");
          refreshTemplatesTab();
        } catch (err) {
          UI.handleError(err, "模板校验未通过");
        }
      });
    });
    body.querySelectorAll("[data-reset]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await API.post(`/api/settings/templates/${btn.dataset.reset}/reset`);
          UI.toast("已恢复默认模板", "success");
          refreshTemplatesTab();
        } catch (err) { UI.handleError(err); }
      });
    });
  }

  // ---------- 数据库备份 ----------

  function formatBytes(size) {
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }

  function backupStatus(item) {
    if (item.valid) return `<span class="badge badge-good">有效</span>`;
    return `<span class="badge badge-critical">无效</span>`;
  }

  function renderCleanupPreview(container, preview) {
    const candidates = preview.candidates || [];
    container.innerHTML = `
      <div class="backup-preview ${candidates.length ? "has-candidates" : "is-safe"}">
        <div class="backup-preview-head">
          <b>${candidates.length ? `发现 ${candidates.length} 个可清理备份` : "当前没有需要清理的备份"}</b>
          <span class="muted">保留最近 ${preview.policy.keep_count} 个，并保留最近 ${preview.policy.keep_days} 天</span>
        </div>
        ${candidates.length ? `
          <div class="backup-preview-list">
            ${candidates.map((item) => `
              <div class="backup-row">
                <span>${UI.esc(item.filename)}</span>
                <span class="muted">${UI.esc(UI.fmtDate(item.modified_at))} · ${formatBytes(item.size)}</span>
              </div>`).join("")}
          </div>
          <div class="form-hint">仅列出有效且超出保留策略的备份，损坏备份不会被自动删除。</div>` : `
          <div class="form-hint">当前共有 ${preview.valid_count} 个有效备份、${preview.invalid_count} 个无效备份，均在保留范围内。</div>`}
      </div>`;
  }

  async function loadCleanupPreview(body) {
    const previewBox = body.querySelector("#backup-cleanup-preview");
    const cleanupBtn = body.querySelector("#btn-cleanup-backups");
    if (!previewBox) return;
    previewBox.innerHTML = `<div class="muted"><span class="spinner" style="width:12px;height:12px;border-width:1.5px"></span> 正在计算清理范围…</div>`;
    if (cleanupBtn) cleanupBtn.disabled = true;
    try {
      const preview = await API.get("/api/settings/database/backups/cleanup-preview");
      renderCleanupPreview(previewBox, preview);
      if (cleanupBtn) {
        cleanupBtn.disabled = !preview.deletable_count;
        cleanupBtn.dataset.candidates = String(preview.deletable_count || 0);
      }
    } catch (err) {
      previewBox.innerHTML = `<div class="alert alert-warning">${UI.esc((err && err.message) || "清理预览加载失败")}</div>`;
    }
  }

  async function refreshDatabaseTab() {
    const body = document.querySelector("#tab-database");
    if (!body) return;
    body.innerHTML = `<div class="card"><div class="muted"><span class="spinner" style="width:12px;height:12px;border-width:1.5px"></span> 正在加载数据库状态…</div></div>`;
    try {
      const [integrity, backupData] = await Promise.all([
        API.get("/api/settings/database/integrity"),
        API.get("/api/settings/database/backups"),
      ]);
      renderDatabase(body, integrity, backupData.items || []);
      loadCleanupPreview(body);
    } catch (err) {
      body.innerHTML = `<div class="alert alert-warning">${UI.esc((err && err.message) || "数据库状态加载失败")}</div>`;
    }
  }

  function renderDatabase(body, integrity, backups) {
    const violations = integrity.foreign_key_violations || [];
    const integrityOk = integrity.ok;
    body.innerHTML = `
      <div class="card database-card">
        <div class="database-head">
          <div>
            <div class="card-title">数据库备份管理</div>
            <div class="muted">检查数据库一致性、创建备份，并按保留策略清理旧备份。</div>
          </div>
          <div class="database-actions">
            <button class="btn btn-sm btn-ghost" id="btn-refresh-database">刷新</button>
            <button class="btn btn-sm btn-primary" id="btn-create-backup">立即备份</button>
          </div>
        </div>
        <div class="database-status ${integrityOk ? "is-ok" : "is-warning"}">
          <span class="status-dot ${integrityOk ? "ok" : "failed"}"></span>
          <div>
            <b>${integrityOk ? "数据库检查正常" : "数据库存在一致性问题"}</b>
            <div class="muted">完整性检查：${UI.esc(integrity.integrity_check || "无法执行") } · 外键问题：${violations.length}</div>
            ${integrity.error ? `<div class="form-hint">${UI.esc(integrity.error)}</div>` : ""}
          </div>
        </div>
      </div>

      <div class="card database-card">
        <div class="database-head">
          <div>
            <div class="card-title">备份文件（${backups.length}）</div>
            <div class="muted">无效备份仅提示，不会被自动清理。</div>
          </div>
        </div>
        <div class="backup-list">
          ${backups.length ? backups.map((item) => `
            <div class="backup-row">
              <div class="backup-file">
                ${backupStatus(item)}
                <span>${UI.esc(item.filename)}</span>
              </div>
              <span class="muted">${UI.esc(UI.fmtDate(item.modified_at))} · ${formatBytes(item.size)}</span>
            </div>`).join("") : `<div class="empty" style="padding:20px">暂无备份文件，点击“立即备份”创建第一份备份。</div>`}
        </div>
      </div>

      <div class="card database-card">
        <div class="database-head">
          <div>
            <div class="card-title">清理旧备份</div>
            <div class="muted">先预览候选文件，确认后才会删除。</div>
          </div>
          <div class="database-actions">
            <button class="btn btn-sm btn-ghost" id="btn-preview-backups">刷新预览</button>
            <button class="btn btn-sm btn-danger" id="btn-cleanup-backups" disabled>确认清理</button>
          </div>
        </div>
        <div id="backup-cleanup-preview"></div>
      </div>`;

    body.querySelector("#btn-refresh-database").addEventListener("click", refreshDatabaseTab);
    body.querySelector("#btn-create-backup").addEventListener("click", async (event) => {
      event.currentTarget.disabled = true;
      try {
        const result = await API.post("/api/settings/database/backup");
        UI.toast(`备份已创建：${result.filename}`, "success");
        refreshDatabaseTab();
      } catch (err) {
        UI.handleError(err, "备份创建失败");
      } finally {
        event.currentTarget.disabled = false;
      }
    });
    body.querySelector("#btn-preview-backups").addEventListener("click", () => loadCleanupPreview(body));
    body.querySelector("#btn-cleanup-backups").addEventListener("click", async (event) => {
      const count = Number(event.currentTarget.dataset.candidates || 0);
      if (!count) {
        UI.toast("当前没有可清理的备份", "info");
        return;
      }
      if (!confirm(`确定清理预览中的 ${count} 个旧备份吗？此操作不可撤销。`)) return;
      event.currentTarget.disabled = true;
      try {
        const result = await API.post("/api/settings/database/backups/cleanup", { confirm: true });
        UI.toast(`已清理 ${result.deleted_count} 个备份`, result.failed_count ? "warning" : "success");
        refreshDatabaseTab();
      } catch (err) {
        UI.handleError(err, "备份清理失败");
        event.currentTarget.disabled = false;
      }
    });
  }

  // ---------- 关于 ----------

  async function refreshAboutTab() {
    const body = document.querySelector("#tab-about");
    if (!body) return;
    try {
      const info = await API.get("/api/settings/app");
      body.innerHTML = `
        <div class="card">
          <div class="kv"><span class="kv-k">应用版本</span><span class="kv-v">${UI.esc(info.version)}</span></div>
          <div class="kv"><span class="kv-k">数据目录</span><span class="kv-v">${UI.esc(info.data_dir)}</span></div>
          <div class="kv"><span class="kv-k">数据库文件</span><span class="kv-v">${UI.esc(info.db_path)}</span></div>
        </div>
        <div class="mt-16">
          <button class="btn btn-ghost" id="btn-open-dir">打开数据目录</button>
        </div>
        <div class="alert alert-info mt-16">
          <div>💡</div>
          <div>数据全部保存在本机 SQLite 文件中，建议定期备份 <b>data/app.db</b>。更换电脑或系统账户后，已保存的 API Key 需重新填写。</div>
        </div>`;
      body.querySelector("#btn-open-dir").addEventListener("click", () => {
        if (window.pywebview && window.pywebview.api && window.pywebview.api.open_data_dir) {
          window.pywebview.api.open_data_dir();
        } else {
          UI.toast("请使用桌面版应用打开数据目录", "warning");
        }
      });
    } catch (err) {
      UI.handleError(err, "加载失败");
    }
  }

  // ---------- 主视图 ----------

  Views.settings = {
    render() {
      const app = document.getElementById("app");
      app.innerHTML = `
        <div class="page">
          <div class="page-head">
            <div>
              <div class="page-title">设置</div>
              <div class="page-sub">模型 Key 配置 · 质检模板权重 · 应用信息</div>
            </div>
          </div>
          <div class="tabs">
            <span class="tab active" data-tab="models">我的模型</span>
            <span class="tab" data-tab="templates">质检模板</span>
            <span class="tab" data-tab="database">数据库</span>
            <span class="tab" data-tab="about">关于</span>
          </div>
          <div id="tab-models"></div>
          <div id="tab-templates" style="display:none"></div>
          <div id="tab-database" style="display:none"></div>
          <div id="tab-about" style="display:none"></div>
        </div>`;
      app.querySelectorAll(".tab").forEach((tab) => {
        tab.addEventListener("click", () => {
          app.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
          tab.classList.add("active");
          const key = tab.dataset.tab;
          ["models", "templates", "database", "about"].forEach((k) => {
            document.getElementById(`tab-${k}`).style.display = k === key ? "" : "none";
          });
          if (key === "templates") refreshTemplatesTab();
          if (key === "database") refreshDatabaseTab();
          if (key === "about") refreshAboutTab();
        });
      });
      refreshModelsTab();
    },
  };
})();
