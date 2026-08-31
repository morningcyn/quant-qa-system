// 质检报告页：总览卡 / 双雷达 / 维度明细 / 错误诊断 vs 黄金修复 / 建议 / 原始对话 / 导出
(function () {
  "use strict";
  window.Views = window.Views || {};

  const C = window.Charts.COLORS;

  // S1 维度命名统一：按业务打分机制文字版口径显示"情绪维度"（历史报告快照可能是"情绪维稳"）
  const S1_NAME = "情绪维度";

  // d_scores/s_scores 的键可能是模板短键（d1）也可能是存储完整键（d1_emotion_change），统一兼容读取
  function pickScore(map, key) {
    if (!map) return {};
    if (Object.prototype.hasOwnProperty.call(map, key)) return map[key] || {};
    const hit = Object.keys(map).find((k) => k.indexOf(key + "_") === 0);
    return (hit && map[hit]) || {};
  }

  function dimProgress(score, max) {
    const pct = max > 0 ? Math.max(0, Math.min(100, (score / max) * 100)) : 0;
    const barCls = pct >= 66 ? "bar-good" : pct >= 33 ? "bar-warning" : "bar-critical";
    return `<div class="progress"><span class="${barCls}" style="width:${pct}%"></span></div>`;
  }

  // N/A 豁免维度：score 为 null 时整卡显示"无法判定"，不渲染进度条（进度条组件在调用方按 isNa 跳过）
  function isNa(data) {
    return !data || data.score == null;
  }

  function dDimCard(key, conf, data, color) {
    const na = isNa(data);
    if (na) {
      return `
        <div class="dim-card">
          <div class="dim-head">
            <span class="dim-name" style="color:${color}">${key.toUpperCase()} ${UI.esc(conf.name)}</span>
            <span class="dim-score" style="color:var(--text-3)">N/A 无法判定</span>
          </div>
          <div class="dim-comment">${UI.esc(data.na_reason || "该维度因文本信息不足未评分（已从总分分母中豁免，按得分率折算）")}</div>
        </div>`;
    }
    const extra = [];
    if (data.rating) extra.push(`<span class="badge badge-blue">${UI.esc(data.rating)}</span>`);
    if (key === "d2_profile_match" && data.profile) extra.push(`<span class="badge badge-neutral">画像：${UI.esc(data.profile)}</span>`);
    if (data.match_rating) extra.push(`<span class="badge badge-blue">${UI.esc(data.match_rating)}</span>`);
    if (key === "d3_problem_match" && data.surface_vs_deep) extra.push(`<span class="badge badge-blue">${UI.esc(data.surface_vs_deep)}</span>`);
    if (data.resolution) extra.push(`<span class="badge badge-neutral">${UI.esc(data.resolution)}</span>`);
    if (key === "d4_expectation_exceed") {
      extra.push(`<span class="badge badge-neutral">预判衍生问题 ${data.derived_question || 0} 个</span>`);
      extra.push(`<span class="badge badge-neutral">掌控感动作 ${data.control_given || 0} 个</span>`);
    }
    const analysisTitle = data.analysis ? `title="${UI.esc(data.analysis)}"` : "";
    return `
      <div class="dim-card" ${analysisTitle}>
        <div class="dim-head">
          <span class="dim-name" style="color:${color}">${key.toUpperCase()} ${UI.esc(conf.name)}</span>
          <span class="dim-score num">${data.score} / ${conf.max}</span>
        </div>
        ${dimProgress(data.score, conf.max)}
        ${extra.length ? `<div class="mt-8" style="display:flex;gap:6px;flex-wrap:wrap">${extra.join("")}</div>` : ""}
        ${data.comment ? `<div class="dim-comment">${UI.esc(data.comment)}</div>` : ""}
      </div>`;
  }

  // 子项 v2 为对象 {analysis, score}，兼容旧格式纯数值
  function subVal(raw) {
    if (raw && typeof raw === "object" && typeof raw.score === "number") return raw.score;
    return Number(raw) || 0;
  }

  function sDimCard(key, conf, data, color) {
    const na = isNa(data);
    if (na) {
      return `
        <div class="dim-card">
          <div class="dim-head">
            <span class="dim-name" style="color:${color}">${key.toUpperCase()} ${UI.esc(key === "s1" ? S1_NAME : conf.name)}</span>
            <span class="dim-score" style="color:var(--text-3)">N/A 无法判定</span>
          </div>
          <div class="dim-comment">${UI.esc(data.na_reason || "该维度因文本信息不足未评分（子项一并豁免，已从总分分母中扣除）")}</div>
        </div>`;
    }
    const subRows = Object.keys(conf.sub_items || {})
      .map((subKey) => {
        const subConf = conf.sub_items[subKey];
        const raw = (data.sub_items && data.sub_items[subKey]) || 0;
        const val = subVal(raw);
        const note = (raw && typeof raw === "object" && raw.analysis) ? raw.analysis : "";
        const pct = subConf.max > 0 ? Math.max(0, Math.min(100, (val / subConf.max) * 100)) : 0;
        const barCls = pct >= 66 ? "bar-good" : pct >= 33 ? "bar-warning" : "bar-critical";
        return `<div class="sub-item-row" title="${UI.esc(note)}">
          <span class="si-name">${UI.esc(subConf.name)}</span>
          <div class="progress"><span class="${barCls}" style="width:${pct}%"></span></div>
          <span class="si-val num">${val}/${subConf.max}</span>
        </div>`;
      })
      .join("");
    return `
      <div class="dim-card">
        <div class="dim-head">
          <span class="dim-name" style="color:${color}">${key.toUpperCase()} ${UI.esc(key === "s1" ? S1_NAME : conf.name)}</span>
          <span class="dim-score num">${data.score} / ${conf.max}</span>
        </div>
        ${dimProgress(data.score, conf.max)}
        <div class="mt-8">${subRows}</div>
        ${data.comment ? `<div class="dim-comment">${UI.esc(data.comment)}</div>` : ""}
      </div>`;
  }

  // 从总览对比页进入的报告 → 返回链接指向该总览（员工主页进入保持原样）
  // 校验方式：localStorage 里的 last_overview 的参与者必须包含本报告，否则视为残留并清除
  async function resolveBackLink(reportId) {
    let stored = null;
    try {
      stored = JSON.parse(localStorage.getItem("last_overview") || "null");
    } catch (e) {
      stored = null;
    }
    if (!stored || !stored.id) return null;
    try {
      const ov = await API.get(`/api/overviews/${stored.id}`);
      const hit = (ov.participants || []).some((p) => p.report && p.report.id === reportId);
      if (hit) return { id: stored.id, title: stored.title || "本次客户服务总览" };
    } catch (e) {
      // 总览不存在或网络失败 → 视为残留
    }
    localStorage.removeItem("last_overview");
    return null;
  }

  // 同会话多位助理（批量评分多助理任务 / 多人质检）：报告页顶部助理切换栏，点谁看谁的完整报告
  function sessionTabBar(r) {
    const reps = r.session_reports || [];
    if (reps.length < 2) return "";
    const current = r.id;
    return `
      <div class="card mb-16">
        <div class="card-title">本次会话参与助理（${reps.length} 位 · 点击切换查看各自报告）</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          ${reps.map((s) => `
            <a class="btn btn-sm ${s.id === current ? "btn-primary" : "btn-ghost"}" href="#/report/${s.id}">
              ${UI.esc(s.assistant_name)} ${s.total_score} 分 ${s.is_red_alert ? "🚫" : s.is_yellow_alert ? "⚠️" : ""}
            </a>`).join("")}
        </div>
      </div>`;
  }

  function renderReportPage(app, r, back) {
    const tpl = r.template_snapshot || {};
    const dConf = tpl.d || {};
    const sConf = tpl.s || {};
    const d = r.d_scores || {};
    const s = r.s_scores || {};
    // N/A 豁免维度（key 短键集合）+ 折算说明
    const naDims = r.na_dims || [];
    const naKeys = new Set(naDims.map((nd) => nd.key));
    const naNote = naDims.length
      ? `（已豁免：${naDims.map((nd) => UI.esc(nd.name)).join("、")}）`
      : "";
    // 状态优先级：红灯（合规一票否决）> 黄灯（业务瑕疵）> 正常
    const statusBadge = r.is_red_alert
      ? `<span class="badge badge-critical">🚫 红灯拦截（合规违规）</span>`
      : r.is_yellow_alert
        ? `<span class="badge badge-warning">⚠️ 黄灯预警</span>`
        : `<span class="badge badge-good">✔ 正常</span>`;
    const heroScore = r.is_red_alert ? "critical" : r.is_yellow_alert ? "warning" : "good";
    // N/A 豁免：总分按有效维度得分率折算（如 62/88 → 70），这里展示折算依据
    const convertNote = r.effective_max && r.effective_max < 100 && r.effective_score != null
      ? `<div style="font-size:12px;color:var(--text-3)">折算自 ${r.effective_score}/${r.effective_max}（豁免 ${r.na_dims.length} 个无法判定维度）</div>`
      : "";
    const redAlertHtml = r.is_red_alert && r.red_alert_reasons && r.red_alert_reasons.length
      ? `<div class="alert alert-critical mt-16"><div>🚫</div><div><b>红灯一票否决：命中合规红线</b><ul>${r.red_alert_reasons.map((x) => `<li>${UI.esc(x)}</li>`).join("")}</ul></div></div>`
      : "";
    const alertHtml = r.is_yellow_alert && r.yellow_alert_reasons && r.yellow_alert_reasons.length
      ? `<div class="alert alert-warning mt-16"><div>⚠️</div><div><b>黄灯熔断已触发（总分低于 59）</b><ul>${r.yellow_alert_reasons.map((x) => `<li>${UI.esc(x)}</li>`).join("")}</ul></div></div>`
      : "";

    const highlightItems = (r.highlight_dialogue || []).map((h, i) => `
      <div class="collapse" data-idx="${i}">
        <div class="collapse-head">
          <span class="collapse-caret">▶</span>
          <span class="badge badge-critical">第 ${h.turn} 轮 · ${h.role === "客" ? "客户" : "助理"}</span>
          <span style="font-size:12px;color:var(--text-2)">${UI.esc(h.issue_type || "扣分话术")}</span>
          <span style="margin-left:auto" class="muted">黄金改写 →</span>
        </div>
        <div class="collapse-body">
          <div class="compare-grid">
            <div class="compare-box compare-original">
              <div class="compare-label">✗ 原话（失分）</div>
              ${UI.esc(h.original_text)}
            </div>
            <div class="compare-box compare-rewrite">
              <div class="compare-label">✓ AI 黄金改写
                <button class="copy-btn" data-copy="${i}">复制</button>
              </div>
              ${UI.esc(h.ai_rewrite)}
            </div>
          </div>
        </div>
      </div>`).join("");

    const suggestions = (r.improvement_suggestions || []).map((x, i) => `
      <div class="suggestion-item"><div class="si-num">${i + 1}</div><div>${UI.esc(x)}</div></div>`).join("");

    const dCards = ["d1", "d2", "d3", "d4"]
      .map((key) => dDimCard(key, dConf[key] || {}, pickScore(d, key), C.dBlue))
      .join("");
    const sCards = ["s1", "s2", "s3"]
      .map((key) => sDimCard(key, sConf[key] || {}, pickScore(s, key), C.sTeal))
      .join("");

    const fileBase = `质检报告_${r.assistant_name}_${r.total_score}分`;

    app.innerHTML = `
      <div class="page report-page">
        <div class="page-head no-export">
          <div class="title-row">
            ${back
              ? `<a class="back-link" href="#/overview/${back.id}">← 返回本次总览对比${back.title ? `（${UI.esc(back.title)}）` : ""}</a>`
              : `<a class="back-link" href="#/assistant/${r.assistant_id}">← 返回 ${UI.esc(r.assistant_name)}</a>`}
            <div>
              <div class="page-title">${r.session_title ? UI.esc(r.session_title) : "质检报告"}</div>
              <div class="page-sub">${UI.esc(r.assistant_name)}（工号 ${UI.esc(r.employee_no)}）· ${UI.fmtDate(r.created_at)}</div>
            </div>
          </div>
          <div class="page-actions">
            <button class="btn btn-ghost" id="btn-png">导出长图 PNG</button>
            <button class="btn btn-ghost" id="btn-pdf">导出 PDF</button>
          </div>
        </div>

        ${sessionTabBar(r)}

        <div class="report-hero">
          <div class="hero-score ${heroScore} num">${r.total_score}<span style="font-size:20px;color:var(--text-3)"> / 100</span></div>
          <div class="hero-meta">
            <div>${statusBadge}</div>
            <div class="hero-tags">
              ${r.customer_profile ? `<span class="tag">👤 客户画像：${UI.esc(r.customer_profile)}</span>` : ""}
              ${r.evaluatee ? `<span class="tag">🎯 评估对象：${UI.esc(r.evaluatee)}</span>` : ""}
              <span class="tag">📋 质检模板：${UI.esc(r.template_name)}</span>
              <span class="tag">💬 对话轮数：${r.turn_count} 轮</span>
            </div>
          </div>
          ${convertNote}
        </div>
        ${redAlertHtml}
        ${alertHtml}

        <div class="grid-2 mt-16">
          <div class="card">
            <div class="card-title">D 端能力雷达（满分 ${(tpl.d ? Object.values(tpl.d).reduce((a, b) => a + b.max, 0) : 55)}）${naDims.filter((nd) => nd.key.startsWith("d")).length ? `<span class="muted" style="font-size:11px">已豁免：${naDims.filter((nd) => nd.key.startsWith("d")).map((nd) => UI.esc(nd.name)).join("、")}</span>` : ""}</div>
            <div id="radar-d" class="chart-box"></div>
          </div>
          <div class="card">
            <div class="card-title">S 端能力雷达（满分 ${(tpl.s ? Object.values(tpl.s).reduce((a, b) => a + b.max, 0) : 45)}）${naDims.filter((nd) => nd.key.startsWith("s")).length ? `<span class="muted" style="font-size:11px">已豁免：${naDims.filter((nd) => nd.key.startsWith("s")).map((nd) => UI.esc(nd.name)).join("、")}</span>` : ""}</div>
            <div id="radar-s" class="chart-box"></div>
          </div>
        </div>

        <h2 class="mt-24 mb-8">维度明细</h2>
        <div class="grid-2">
          <div>${dCards}</div>
          <div>${sCards}</div>
        </div>

        <h2 class="mt-24 mb-8">错误诊断 vs AI 黄金修复</h2>
        <div class="toolbar no-export" style="margin-bottom:0">
          <button class="btn btn-sm btn-ghost" id="btn-expand-all">展开全部</button>
          <span class="muted" style="font-size:12px">共 ${(r.highlight_dialogue || []).length} 处扣分话术</span>
        </div>
        <div class="mt-8">
          ${highlightItems || `<div class="empty">🎉 本次会话没有明显扣分话术</div>`}
        </div>

        <h2 class="mt-24 mb-8">改进建议</h2>
        ${suggestions || `<div class="empty">暂无建议</div>`}

        <h2 class="mt-24 mb-8">原始对话</h2>
        <div class="collapse" id="collapse-raw">
          <div class="collapse-head">
            <span class="collapse-caret">▶</span>
            <span style="font-size:13px;color:var(--text-2)">查看本次会话原文</span>
          </div>
          <div class="collapse-body">
            <div class="raw-dialogue">${UI.esc(r.raw_dialogue)}</div>
          </div>
        </div>

        <div class="sticky-actions">
          <a class="btn btn-ghost" href="#/assistant/${r.assistant_id}">返回员工主页</a>
          <button class="btn btn-primary" id="btn-png2">导出长图 PNG</button>
          <button class="btn btn-ghost" id="btn-pdf2">导出 PDF</button>
        </div>
      </div>`;

    // 雷达图（N/A 豁免维度剔除指标，避免 null 破坏雷达图形）
    if (window.echarts) {
      const dKeys = ["d1", "d2", "d3", "d4"].filter((k) => !naKeys.has(k));
      const sKeys = ["s1", "s2", "s3"].filter((k) => !naKeys.has(k));
      const dInd = dKeys.map((k) => ({ name: (dConf[k] || {}).name || k, max: (dConf[k] || {}).max || 10 }));
      const dVals = dKeys.map((k) => ({ value: (pickScore(d, k).score) || 0 }));
      const sInd = sKeys.map((k) => ({ name: (k === "s1" ? S1_NAME : (sConf[k] || {}).name) || k, max: (sConf[k] || {}).max || 15 }));
      const sVals = sKeys.map((k) => ({ value: (pickScore(s, k).score) || 0 }));
      Charts.renderRadar(document.getElementById("radar-d"), dInd, [{ name: "D端得分", values: dVals, color: C.dBlue }]);
      Charts.renderRadar(document.getElementById("radar-s"), sInd, [{ name: "S端得分", values: sVals, color: C.sTeal }]);
    }

    // 折叠交互
    app.querySelectorAll(".collapse-head").forEach((head) => {
      head.addEventListener("click", () => head.closest(".collapse").classList.toggle("open"));
    });
    const expandAll = document.getElementById("btn-expand-all");
    if (expandAll) {
      expandAll.addEventListener("click", () => {
        const allOpen = app.querySelectorAll(".collapse").length === app.querySelectorAll(".collapse.open").length;
        app.querySelectorAll(".collapse").forEach((c) => c.classList.toggle("open", !allOpen));
        expandAll.textContent = allOpen ? "展开全部" : "收起全部";
      });
    }

    // 复制改写
    app.querySelectorAll("[data-copy]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const idx = Number(btn.dataset.copy);
        const text = (r.highlight_dialogue || [])[idx].ai_rewrite;
        navigator.clipboard.writeText(text).then(
          () => UI.toast("已复制黄金话术", "success"),
          () => UI.toast("复制失败，请手动选择复制", "error")
        );
      });
    });

    // 从总览进入的报告：点「返回员工主页」视为离开总览语境，清除返回标记
    app.querySelectorAll('.sticky-actions a[href^="#/assistant/"]').forEach((a) => {
      a.addEventListener("click", () => localStorage.removeItem("last_overview"));
    });

    // 导出
    const root = app.querySelector(".report-page");
    const doPNG = () => Exporter.exportPNG(root, `${fileBase}.png`).then((m) => m && UI.toast(m, "success")).catch((e) => UI.handleError(e, "导出失败"));
    const doPDF = () => Exporter.exportPDF(root, `${fileBase}.pdf`).then((m) => m && UI.toast(m, "success")).catch((e) => UI.handleError(e, "导出失败"));
    document.getElementById("btn-png").addEventListener("click", doPNG);
    document.getElementById("btn-png2").addEventListener("click", doPNG);
    document.getElementById("btn-pdf").addEventListener("click", doPDF);
    document.getElementById("btn-pdf2").addEventListener("click", doPDF);
  }

  Views.report = {
    async render(id) {
      const app = document.getElementById("app");
      app.innerHTML = `<div class="empty"><div class="spinner" style="margin-bottom:12px"></div>正在加载报告…</div>`;
      try {
        const r = await API.get(`/api/reports/${id}`);
        const back = await resolveBackLink(r.id);
        renderReportPage(app, r, back);
      } catch (err) {
        app.innerHTML = `
          <div class="empty">
            <div class="empty-icon">⚠️</div>
            <div>${UI.esc((err && err.message) || "报告加载失败")}</div>
            <div class="mt-16"><a class="btn btn-ghost" href="#/assistants">返回员工列表</a></div>
          </div>`;
      }
    },
  };
})();
