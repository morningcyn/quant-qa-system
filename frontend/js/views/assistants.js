// 员工列表页：搜索 / 模板筛选 / 新增 / 编辑 / 删除 / 卡片统计
(function () {
  "use strict";
  window.Views = window.Views || {};

  const TEMPLATE_NAMES = { standard: "标准", newbie: "新人", vip: "高净值" };
  const TEMPLATE_BADGE = {
    standard: `<span class="badge badge-blue">标准</span>`,
    newbie: `<span class="badge badge-teal">新人</span>`,
    vip: `<span class="badge badge-warning">高净值</span>`,
  };

  function openFormModal(existing) {
    const isEdit = !!existing;
    const form = document.createElement("div");
    form.innerHTML = `
      <div class="form-row">
        <label class="form-label">姓名 *</label>
        <input class="input" id="f-name" maxlength="100" value="${existing ? UI.esc(existing.name) : ""}" placeholder="例如：张三">
      </div>
      <div class="form-row">
        <label class="form-label">工号 *${isEdit ? "（不可修改）" : ""}</label>
        <input class="input" id="f-no" maxlength="50" value="${existing ? UI.esc(existing.employee_no) : ""}" ${isEdit ? "disabled" : ""} placeholder="例如：E001">
      </div>
      <div class="form-row">
        <label class="form-label">质检模板</label>
        <select class="select" id="f-tpl">
          <option value="standard" ${!existing || existing.template_type === "standard" ? "selected" : ""}>标准服务模板</option>
          <option value="newbie" ${existing && existing.template_type === "newbie" ? "selected" : ""}>新人辅导模板</option>
          <option value="vip" ${existing && existing.template_type === "vip" ? "selected" : ""}>高净值客户模板</option>
        </select>
        <div class="form-hint">模板决定评分尺度，可在「设置 → 质检模板」中调整权重</div>
      </div>
      <div class="form-row">
        <label class="form-label">老师人设（可选）</label>
        <textarea class="input" id="f-persona" rows="3" maxlength="2000" placeholder="例如：王老师，深耕技术面十余年，擅长均线与支撑位分析，风格权威笃定、用「你」平视交流，从不承诺收益、不报具体点位。">${existing && existing.teacher_persona ? UI.esc(existing.teacher_persona) : ""}</textarea>
        <div class="form-hint">质检时注入提示词：打分与黄金改写都将代入这位老师的视角、语气与风控纪律</div>
      </div>
      <div class="form-actions">
        <button class="btn btn-ghost" id="f-cancel">取消</button>
        <button class="btn btn-primary" id="f-save">${isEdit ? "保存" : "新增员工"}</button>
      </div>`;
    const modal = UI.openModal({ title: isEdit ? "编辑员工" : "新增员工", content: form });
    modal.body.querySelector("#f-cancel").addEventListener("click", modal.close);
    modal.body.querySelector("#f-save").addEventListener("click", async () => {
      const name = modal.body.querySelector("#f-name").value.trim();
      const employee_no = isEdit ? existing.employee_no : modal.body.querySelector("#f-no").value.trim();
      const template_type = modal.body.querySelector("#f-tpl").value;
      const teacher_persona = modal.body.querySelector("#f-persona").value.trim();
      if (!name || !employee_no) { UI.toast("请填写姓名和工号", "warning"); return; }
      try {
        if (isEdit) await API.put(`/api/assistants/${existing.id}`, { name, template_type, teacher_persona });
        else await API.post("/api/assistants", { name, employee_no, template_type, teacher_persona });
        modal.close();
        UI.toast(isEdit ? "已保存" : `员工「${name}」已创建`, "success");
        Views.assistants.render();
      } catch (err) {
        UI.handleError(err, "保存失败");
      }
    });
  }

  function confirmDelete(a) {
    const content = document.createElement("div");
    content.innerHTML = `
      <div class="alert alert-critical">
        <div>⚠️</div>
        <div>确定删除员工 <b>${UI.esc(a.name)}</b>（工号 ${UI.esc(a.employee_no)}）？<br>
        该员工的全部历史质检记录将一并删除，且无法恢复。</div>
      </div>
      <div class="form-actions">
        <button class="btn btn-ghost" id="d-cancel">取消</button>
        <button class="btn btn-danger" id="d-confirm">确认删除</button>
      </div>`;
    const modal = UI.openModal({ title: "删除员工", content });
    modal.body.querySelector("#d-cancel").addEventListener("click", modal.close);
    modal.body.querySelector("#d-confirm").addEventListener("click", async () => {
      try {
        await API.del(`/api/assistants/${a.id}`);
        modal.close();
        UI.toast("员工已删除", "success");
        Views.assistants.render();
      } catch (err) {
        UI.handleError(err, "删除失败");
      }
    });
  }

  function renderList(app, data, query, filter) {
    const assistants = data.assistants || [];
    const chips = [
      { key: "", label: "全部" },
      { key: "standard", label: "标准" },
      { key: "newbie", label: "新人" },
      { key: "vip", label: "高净值" },
    ];
    const cards = assistants.length
      ? assistants.map((a) => {
          const st = a.stats || {};
          const avg = st.avg_score === null || st.avg_score === undefined ? "—" : `<b>${st.avg_score}</b>`;
          return `
          <div class="assistant-card" data-id="${a.id}">
            <div class="ac-head">
              <div>
                <div class="ac-name">${UI.esc(a.name)}</div>
                <div class="ac-no">工号 ${UI.esc(a.employee_no)}</div>
              </div>
              ${TEMPLATE_BADGE[a.template_type] || TEMPLATE_BADGE.standard}
            </div>
            <div class="ac-stats">
              <span>近30天均分 ${avg}</span>
              <span>质检 <b>${st.count || 0}</b> 次</span>
              <span>黄灯 <b style="color:${st.yellow_count ? "var(--warning)" : "inherit"}">${st.yellow_count || 0}</b></span>
            </div>
          </div>`;
        }).join("")
      : `<div class="empty" style="grid-column:1/-1">
          <div class="empty-icon">👥</div>
          还没有员工，点击右上角「新增员工」开始使用
        </div>`;

    app.innerHTML = `
      <div class="page">
        <div class="page-head">
          <div>
            <div class="page-title">员工管理</div>
            <div class="page-sub">管理下属员工，点击员工查看质检档案</div>
          </div>
          <div class="page-actions">
            <button class="btn btn-primary" id="btn-new">＋ 新增员工</button>
          </div>
        </div>
        <div class="toolbar">
          <input class="input" id="q" placeholder="搜索姓名 / 工号…" value="${UI.esc(query)}">
          ${chips.map((c) => `<span class="chip ${filter === c.key ? "active" : ""}" data-filter="${c.key}">${c.label}</span>`).join("")}
        </div>
        <div class="assistant-grid">${cards}</div>
      </div>`;

    app.querySelector("#btn-new").addEventListener("click", () => openFormModal(null));
    app.querySelectorAll(".assistant-card").forEach((card) => {
      card.addEventListener("click", () => { location.hash = `#/assistant/${card.dataset.id}`; });
    });
    app.querySelectorAll("[data-filter]").forEach((chip) => {
      chip.addEventListener("click", () => Views.assistants.render(chip.dataset.filter, app.querySelector("#q").value.trim()));
    });
    let timer = null;
    app.querySelector("#q").addEventListener("input", (e) => {
      clearTimeout(timer);
      timer = setTimeout(() => Views.assistants.render(filter, e.target.value.trim()), 300);
    });
  }

  Views.assistants = {
    async render(filter, query) {
      filter = filter || "";
      query = query || "";
      const app = document.getElementById("app");
      app.innerHTML = `<div class="empty"><div class="spinner" style="margin-bottom:12px"></div>正在加载…</div>`;
      try {
        const params = new URLSearchParams();
        if (query) params.set("q", query);
        if (filter) params.set("template_type", filter);
        const data = await API.get(`/api/assistants?${params.toString()}`);
        renderList(app, data, query, filter);
      } catch (err) {
        app.innerHTML = `<div class="empty"><div class="empty-icon">⚠️</div>${UI.esc((err && err.message) || "加载失败")}</div>`;
      }
    },
  };
})();
