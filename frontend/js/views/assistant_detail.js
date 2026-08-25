// 下属主页：员工信息 / KPI 四格 / 30天趋势 / Top3 失分 / 历史质检列表
(function () {
  "use strict";
  window.Views = window.Views || {};

  const C = window.Charts.COLORS;
  const PAGE_SIZE = 10;

  const TEMPLATE_NAMES = { standard: "标准", newbie: "新人", vip: "高净值" };

  function openEditModal(a) {
    const form = document.createElement("div");
    form.innerHTML = `
      <div class="form-row">
        <label class="form-label">姓名 *</label>
        <input class="input" id="e-name" maxlength="100" value="${UI.esc(a.name)}">
      </div>
      <div class="form-row">
        <label class="form-label">工号（不可修改）</label>
        <input class="input" value="${UI.esc(a.employee_no)}" disabled>
      </div>
      <div class="form-row">
        <label class="form-label">质检模板</label>
        <select class="select" id="e-tpl">
          <option value="standard" ${a.template_type === "standard" ? "selected" : ""}>标准服务模板</option>
          <option value="newbie" ${a.template_type === "newbie" ? "selected" : ""}>新人辅导模板</option>
          <option value="vip" ${a.template_type === "vip" ? "selected" : ""}>高净值客户模板</option>
        </select>
        <div class="form-hint">修改模板只影响后续质检，历史报告不变</div>
      </div>
      <div class="form-actions">
        <button class="btn btn-ghost" id="e-cancel">取消</button>
        <button class="btn btn-primary" id="e-save">保存</button>
      </div>`;
    const modal = UI.openModal({ title: "编辑员工", content: form });
    modal.body.querySelector("#e-cancel").addEventListener("click", modal.close);
    modal.body.querySelector("#e-save").addEventListener("click", async () => {
      const name = modal.body.querySelector("#e-name").value.trim();
      if (!name) { UI.toast("请填写姓名", "warning"); return; }
      try {
        await API.put(`/api/assistants/${a.id}`, { name, template_type: modal.body.querySelector("#e-tpl").value });
        modal.close();
        UI.toast("已保存", "success");
        Views.assistantDetail.render(a.id);
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
        <div>确定删除员工 <b>${UI.esc(a.name)}</b>？全部历史质检将一并删除，无法恢复。</div>
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
        location.hash = "#/assistants";
      } catch (err) {
        UI.handleError(err, "删除失败");
      }
    });
  }

  function statTile(label, value, extra) {
    return `
      <div class="stat-tile">
        <div class="stat-label">${label}</div>
        <div class="stat-value num">${value}</div>
        ${extra ? `<div class="stat-extra">${extra}</div>` : ""}
      </div>`;
  }

  function renderInspections(app, a, items, total, page) {
    const rows = items.map((i) => `
      <tr class="clickable" data-id="${i.id}">
        <td>${i.session_title ? UI.esc(i.session_title) : '<span class="muted">（无标题）</span>'}</td>
        <td class="muted">${UI.fmtDate(i.created_at)}</td>
        <td>${i.customer_profile ? `<span class="tag">${UI.esc(i.customer_profile)}</span>` : '<span class="muted">—</span>'}</td>
        <td class="muted num">${i.turn_count} 轮</td>
        <td><b class="num" style="color:${i.is_yellow_alert ? "var(--warning)" : "var(--text)"}">${i.total_score}</b></td>
        <td>${i.is_yellow_alert ? '<span class="badge badge-warning">⚠️ 黄灯</span>' : '<span class="badge badge-good">✔ 正常</span>'}</td>
        <td>
          <a href="#/report/${i.id}" style="font-size:12px">查看报告</a>
          <span style="color:var(--axis);margin:0 6px">|</span>
          <a href="javascript:;" class="del-link" data-id="${i.id}" data-title="${i.session_title ? UI.esc(i.session_title) : "该条质检"}" style="font-size:12px;color:var(--critical)">删除</a>
        </td>
      </tr>`).join("");
    const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    app.querySelector("#hist-table").innerHTML = `
      <table class="table">
        <thead><tr>
          <th>会话标题</th><th>质检时间</th><th>客户画像</th><th>轮数</th><th>总分</th><th>状态</th><th>操作</th>
        </tr></thead>
        <tbody>${rows || '<tr><td colspan="7" class="empty" style="padding:32px">暂无质检记录，点击上方「上传会话质检」开始</td></tr>'}</tbody>
      </table>
      <div class="pagination">
        <span>共 ${total} 条</span>
        <span class="pg-btns">
          <button class="btn btn-sm btn-ghost" id="pg-prev" ${page <= 1 ? "disabled" : ""}>上一页</button>
          <span class="muted">${page} / ${totalPages}</span>
          <button class="btn btn-sm btn-ghost" id="pg-next" ${page >= totalPages ? "disabled" : ""}>下一页</button>
        </span>
      </div>`;
    app.querySelector("#hist-table").querySelectorAll("tr.clickable").forEach((tr) => {
      tr.addEventListener("click", (e) => {
        if (e.target.closest("a")) return;
        location.hash = `#/report/${tr.dataset.id}`;
      });
    });
    const prev = app.querySelector("#pg-prev");
    const next = app.querySelector("#pg-next");
    if (prev) prev.addEventListener("click", () => Views.assistantDetail.render(a.id, page - 1));
    if (next) next.addEventListener("click", () => Views.assistantDetail.render(a.id, page + 1));
    app.querySelectorAll(".del-link").forEach((link) => {
      link.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm(`确定删除质检记录「${link.dataset.title}」？`)) return;
        try {
          await API.del(`/api/inspections/${link.dataset.id}`);
          UI.toast("已删除", "success");
          Views.assistantDetail.render(a.id, page);
        } catch (err) {
          UI.handleError(err, "删除失败");
        }
      });
    });
  }

  function renderPage(app, a, trend, top3, hist, page) {
    const kpi = [
      statTile("近30天均分", trend.total_avg === null || trend.total_avg === undefined ? "—" : trend.total_avg, `共 ${trend.total_count} 次质检`),
      statTile("质检总次数", trend.total_count, `近 ${trend.days} 天统计`),
      statTile("黄灯预警", trend.yellow_count, "总分低于 59 分"),
      statTile("最近一次得分", trend.latest_score === null || trend.latest_score === undefined ? "—" : trend.latest_score, "最新质检结果"),
    ];
    const dims = top3.dimensions || [];
    const subs = top3.sub_items || [];
    const histItems = (hist && hist.items) || [];
    const histTotal = (hist && hist.total) || 0;

    app.innerHTML = `
      <div class="page">
        <div class="page-head">
          <div class="title-row">
            <a class="back-link" href="#/assistants">← 员工列表</a>
            <div>
              <div class="page-title">${UI.esc(a.name)}</div>
              <div class="page-sub">工号 ${UI.esc(a.employee_no)} · ${TEMPLATE_NAMES[a.template_type] || a.template_type}模板</div>
            </div>
          </div>
          <div class="page-actions">
            <button class="btn btn-ghost" id="btn-edit">编辑</button>
            <button class="btn btn-danger" id="btn-del">删除</button>
            <button class="btn btn-primary" id="btn-upload">＋ 上传会话质检</button>
          </div>
        </div>

        <div class="grid-4">${kpi.join("")}</div>

        <div class="card mt-16">
          <div class="card-title">近 30 天均分走势</div>
          <div id="chart-trend" class="chart-box"></div>
        </div>

        <div class="grid-2 mt-16">
          <div class="card">
            <div class="card-title">历史失分最高 Top3（维度）</div>
            <div id="chart-top3-dim" class="chart-box-sm"></div>
          </div>
          <div class="card">
            <div class="card-title">历史失分最高 Top3（S 端子项）</div>
            <div id="chart-top3-sub" class="chart-box-sm"></div>
          </div>
        </div>

        <div class="card mt-16">
          <div class="card-title">历史质检记录</div>
          <div id="hist-table"></div>
        </div>
      </div>`;

    app.querySelector("#btn-upload").addEventListener("click", () => Views.upload.open(a));
    app.querySelector("#btn-edit").addEventListener("click", () => openEditModal(a));
    app.querySelector("#btn-del").addEventListener("click", () => confirmDelete(a));

    if (window.echarts) {
      Charts.renderTrend(document.getElementById("chart-trend"), trend.points, 59);
      if (dims.length) Charts.renderTop3Bar(document.getElementById("chart-top3-dim"), dims, C.dBlue);
      else document.getElementById("chart-top3-dim").innerHTML = `<div class="empty">暂无数据</div>`;
      if (subs.length) Charts.renderTop3Bar(document.getElementById("chart-top3-sub"), subs, C.sTeal);
      else document.getElementById("chart-top3-sub").innerHTML = `<div class="empty">暂无数据</div>`;
    }
    renderInspections(app, a, histItems, histTotal, page);
  }

  Views.assistantDetail = {
    async render(id, page) {
      page = page || 1;
      const app = document.getElementById("app");
      app.innerHTML = `<div class="empty"><div class="spinner" style="margin-bottom:12px"></div>正在加载…</div>`;
      try {
        const [a, trend, top3, hist] = await Promise.all([
          API.get(`/api/assistants/${id}`),
          API.get(`/api/assistants/${id}/stats/trend?days=30`),
          API.get(`/api/assistants/${id}/stats/top3?days=30`),
          API.get(`/api/inspections?assistant_id=${id}&page=${page}&page_size=${PAGE_SIZE}`),
        ]);
        renderPage(app, a, trend, top3, hist, page);
      } catch (err) {
        app.innerHTML = `
          <div class="empty">
            <div class="empty-icon">⚠️</div>
            <div>${UI.esc((err && err.message) || "加载失败")}</div>
            <div class="mt-16"><a class="btn btn-ghost" href="#/assistants">返回员工列表</a></div>
          </div>`;
      }
    },
  };
})();
