// 本次客户服务总览页（对比看板）：分数对比 / 每位助理优缺点（可借鉴·可改正）/ 会话级三卡 / 完整原始聊天记录
(function () {
  "use strict";
  window.Views = window.Views || {};

  function scoreProgress(score) {
    const pct = Math.max(0, Math.min(100, score || 0));
    const barCls = pct >= 66 ? "bar-good" : pct >= 33 ? "bar-warning" : "bar-critical";
    return `<div class="progress"><span class="${barCls}" style="width:${pct}%"></span></div>`;
  }

  function statusBadge(p) {
    if (p.is_red_alert) return `<span class="badge badge-critical">🚫 红灯</span>`;
    if (p.is_yellow_alert) return `<span class="badge badge-warning">⚠️ 黄灯</span>`;
    return `<span class="badge badge-good">✔ 正常</span>`;
  }

  function resolvedBadge(v) {
    if (v === "是") return `<span class="badge badge-good">✔ ${UI.esc(v)}</span>`;
    if (v === "部分") return `<span class="badge badge-warning">◐ ${UI.esc(v)}</span>`;
    if (v === "否") return `<span class="badge badge-critical">✖ ${UI.esc(v)}</span>`;
    return `<span class="badge badge-neutral">？ ${UI.esc(v || "无法判断")}</span>`;
  }

  function summaryCard(title, icon, items) {
    const list = (items || []).length
      ? `<ul style="margin:0;padding-left:18px">${items.map((x) => `<li>${UI.esc(x)}</li>`).join("")}</ul>`
      : `<div class="muted">—</div>`;
    return `
      <div class="card" style="flex:1;min-width:260px">
        <div class="card-title">${icon} ${title}</div>
        <div class="mt-8">${list}</div>
      </div>`;
  }

  // 按分数降序（同分按姓名）→ 排名徽章（并列同分同名次）
  function rankedParticipants(participants) {
    const sorted = [...participants].sort(
      (a, b) => (b.total_score || 0) - (a.total_score || 0) || a.name.localeCompare(b.name, "zh")
    );
    const ranked = [];
    sorted.forEach((p, i) => {
      const rank = i > 0 && sorted[i - 1].total_score === p.total_score ? ranked[i - 1].rank : i + 1;
      ranked.push({ ...p, rank });
    });
    return ranked;
  }

  // 可改正（缺点）：红灯原因 → 黄灯原因 → 扣分话术前 2 → 改进建议前 1；无项给占位
  function buildWeaknesses(p) {
    const items = [];
    for (const x of p.red_alert_reasons || []) items.push(`红灯：${x}`);
    for (const x of p.yellow_alert_reasons || []) items.push(`黄灯：${x}`);
    for (const q of (p.top_issue_quotes || []).slice(0, 2)) items.push(q);
    for (const s of (p.suggestions || []).slice(0, 1)) items.push(s);
    return items.length ? items.slice(0, 3) : ["无明显扣分项"];
  }

  function assistantCard(p) {
    const r = p.report;
    const strengths = (p.strengths || []).slice(0, 3);
    const weaknesses = buildWeaknesses(p);
    return `
      <div class="card">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <span class="badge badge-blue">#${p.rank}</span>
          <span style="font-size:16px;font-weight:600">${UI.esc(p.name)}</span>
          ${statusBadge(p)}
          <span class="num" style="font-size:24px;font-weight:700;margin-left:auto">${p.total_score}<span style="font-size:13px;color:var(--text-3)"> / 100</span></span>
        </div>
        <div class="muted" style="font-size:12px;margin-top:4px">工号 ${UI.esc(p.employee_no || "-")} · 回复 ${p.reply_count} 次 · 轮次 ${UI.esc(p.turn_range || "-")}${p.customer_profile ? ` · 客户画像 ${UI.esc(p.customer_profile)}` : ""}</div>
        <div style="display:flex;gap:24px;flex-wrap:wrap;margin-top:10px">
          <div style="flex:1;min-width:240px">
            <div style="font-size:13px;font-weight:600;margin-bottom:6px">🌟 可借鉴</div>
            <ul style="margin:0;padding-left:18px;font-size:13px">${strengths.map((x) => `<li style="margin-bottom:3px">${UI.esc(x)}</li>`).join("") || `<li class="muted">—</li>`}</ul>
          </div>
          <div style="flex:1;min-width:240px">
            <div style="font-size:13px;font-weight:600;margin-bottom:6px">⚠️ 可改正</div>
            <ul style="margin:0;padding-left:18px;font-size:13px">${weaknesses.map((x) => `<li style="margin-bottom:3px">${UI.esc(x)}</li>`).join("")}</ul>
          </div>
        </div>
        <div style="margin-top:12px">
          ${r ? `<a class="btn btn-ghost" href="#/report/${r.id}">查看完整报告</a>` : `<span class="muted" style="font-size:13px">报告已删除</span>`}
        </div>
      </div>`;
  }

  Views.overview = {
    async render(id) {
      const app = document.getElementById("app");
      app.innerHTML = `<div class="page-head"><div><div class="page-title">本次客户服务总览</div><div class="page-sub">正在加载…</div></div></div>`;
      let data;
      try {
        data = await API.get(`/api/overviews/${id}`);
      } catch (err) {
        UI.handleError(err, "加载总览失败");
        app.innerHTML = `<div class="page-head"><div><div class="page-title">本次客户服务总览</div></div></div>`;
        return;
      }
      // 报告页「← 返回本次总览对比」的锚点（写入失败不影响展示）
      try {
        localStorage.setItem("last_overview", JSON.stringify({ id: data.id, title: data.title || "本次客户服务总览" }));
      } catch (e) { /* ignore */ }

      const summary = data.summary || {};
      const participants = data.participants || [];
      const ranked = rankedParticipants(participants);
      const degradedNote = data.degraded
        ? `<div class="alert alert-warning mb-8"><div>⚠️</div><div>本次总览为<b>规则自动生成</b>（汇总模型调用失败时降级），仅供参考，请结合各助理质检报告人工复核。</div></div>`
        : "";

      const avg = (participants.reduce((a, p) => a + (p.total_score || 0), 0) / Math.max(1, participants.length)).toFixed(1);
      const topScore = Math.max(0, ...participants.map((p) => p.total_score || 0));
      const compareRows = ranked.map((p) => `
        <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--axis)">
          <span class="badge badge-blue">#${p.rank}</span>
          <span style="min-width:88px"><b>${UI.esc(p.name)}</b></span>
          <span class="num" style="min-width:74px">${p.total_score} 分</span>
          ${statusBadge(p)}
          <div style="flex:1;max-width:300px">${scoreProgress(p.total_score)}</div>
        </div>`).join("");
      const cards = ranked.map((p) => assistantCard(p)).join("");

      const rawHtml = (data.raw_dialogue || "").split("\n").map((line) =>
        `<div style="padding:1px 0">${UI.esc(line) || "&nbsp;"}</div>`
      ).join("");

      app.innerHTML = `
        <div class="page-head">
          <div>
            <div class="page-title">${UI.esc(data.title || "本次客户服务总览")}</div>
            <div class="page-sub">会话 ID：${UI.esc(data.conversation_id)} · 生成时间：${UI.fmtDate(data.created_at)} · 参与助理 ${participants.length} 人</div>
          </div>
        </div>
        ${degradedNote}

        <div class="card">
          <div class="card-title">分数对比（${participants.length} 位助理）</div>
          <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px">
            <div class="stat-tile"><div class="stat-value num">${participants.length}</div><div class="stat-label">参与助理</div></div>
            <div class="stat-tile"><div class="stat-value num">${avg}</div><div class="stat-label">平均分</div></div>
            <div class="stat-tile"><div class="stat-value num">${topScore}</div><div class="stat-label">最高分</div></div>
          </div>
          ${participants.length ? compareRows : `<div class="muted">无有效报告</div>`}
        </div>

        <div style="display:flex;flex-direction:column;gap:12px;margin-top:16px">
          ${cards}
        </div>

        <div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:16px">
          ${summaryCard("主要优点", "🌟", summary.main_strengths)}
          ${summaryCard("主要问题", "⚠️", summary.main_issues)}
          <div class="card" style="flex:1;min-width:260px">
            <div class="card-title">❓ 客户问题是否解决</div>
            <div class="mt-8">${resolvedBadge(summary.customer_issue_resolved)}</div>
            ${summary.resolution_reason ? `<div class="mt-8" style="font-size:13px;color:var(--text-2)">${UI.esc(summary.resolution_reason)}</div>` : ""}
            ${summary.overall_comment ? `<div class="mt-8" style="font-size:13px">${UI.esc(summary.overall_comment)}</div>` : ""}
          </div>
        </div>

        <div class="card mt-16">
          <div class="collapse">
            <div class="collapse-head">
              <span class="collapse-caret">▶</span>
              <span>完整原始聊天记录</span>
              <span class="muted" style="margin-left:auto">${(data.raw_dialogue || "").split("\n").length} 行 · 可追溯</span>
            </div>
            <div class="collapse-body" style="display:none;max-height:480px;overflow-y:auto">
              <pre style="white-space:pre-wrap;font-family:inherit;margin:0">${rawHtml}</pre>
            </div>
          </div>
        </div>`;

      // 折叠展开
      const col = app.querySelector(".collapse");
      col.querySelector(".collapse-head").addEventListener("click", () => {
        const body = col.querySelector(".collapse-body");
        const caret = col.querySelector(".collapse-caret");
        const open = body.style.display !== "none";
        body.style.display = open ? "none" : "";
        caret.textContent = open ? "▶" : "▼";
      });
    },
  };
})();
