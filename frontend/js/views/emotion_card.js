// 客户情绪分析卡：报告页内嵌（四态：加载中 / 未生成 / 不可用 / 数据）
// 取数走独立端点（reports.py 零改动）：GET /api/emotion/inspection/{id}，生成走 POST /api/emotion/analyze
(function () {
  "use strict";
  window.EmotionCard = window.EmotionCard || {};

  const CONF_THRESHOLD = 0.5; // 与后端 derive.LOW_CONFIDENCE_THRESHOLD 一致

  // 情绪 → 展示徽章色（后端 EMOTION_LEVEL 同口径）
  const LEVEL = {
    "积极/认可": "good",
    "中性": "neutral",
    "担忧": "warning",
    "怀疑": "warning",
    "失望": "critical",
    "焦虑": "critical",
    "不满": "critical",
    "愤怒": "critical",
  };
  const EMOJI = {
    "积极/认可": "😄", "中性": "😐", "担忧": "😟", "焦虑": "😰",
    "不满": "😠", "愤怒": "🤬", "失望": "😞", "怀疑": "🤨",
  };
  const CHANGE_LABEL = {
    improved: { text: "→ 改善", cls: "good" },
    unchanged: { text: "→ 无变化", cls: "neutral" },
    worsened: { text: "→ 恶化", cls: "critical" },
  };
  // 情绪分值（与后端 derive.EMOTION_SCORE 一致；旧数据 timeline 缺 score 时兜底）
  const SCORE_MAP = {
    "积极/认可": 2, "中性": 0, "担忧": -1, "怀疑": -1,
    "失望": -2, "焦虑": -2, "不满": -3, "愤怒": -4,
  };
  // 曲线调色板随深浅色主题注入（Theme.colors 读 CSS 变量 + 派生色）
  const PALETTE = window.Theme.colors();
  const CURVE_COLORS = {
    good: PALETTE.good, neutral: PALETTE.text3, warning: PALETTE.warning, critical: PALETTE.critical,
    assistant: PALETTE.dBlue, turning: PALETTE.warning, risk: PALETTE.critical,
    text2: PALETTE.text2, text3: PALETTE.text3, grid: PALETTE.grid, axis: PALETTE.axis,
    dotBorder: PALETTE.dotBorder, riskLight: PALETTE.riskLight,
  };
  // 助理回复效果色：改善绿 / 恶化红 / 无变化灰 / 无法评估蓝
  const CHANGE_COLOR = {
    improved: PALETTE.good, worsened: PALETTE.critical,
    unchanged: PALETTE.text3, unknown: PALETTE.dBlue,
  };
  const CHANGE_TXT = {
    improved: "改善", worsened: "恶化", unchanged: "无变化", unknown: "无法评估",
  };
  let curveChart = null; // 当前曲线图实例（重渲染时 dispose，resize 全局只注册一次）

  function scoreOf(it) {
    return it && it.emotion_score != null ? it.emotion_score : (it ? (SCORE_MAP[it.emotion] ?? 0) : 0);
  }

  function scoreColor(s) {
    return s >= 1 ? CURVE_COLORS.good : s === 0 ? CURVE_COLORS.neutral : s >= -1 ? CURVE_COLORS.warning : CURVE_COLORS.critical;
  }

  function fmtTs(ts) {
    if (!ts) return "";
    const s = String(ts);
    return s.length >= 16 ? s.slice(0, 16) : s; // "2026-08-01 10:02:00" → 到分钟
  }

  function changeBadge(chg) {
    const m = {
      improved: ["改善", "good"], unchanged: ["无变化", "neutral"],
      worsened: ["恶化", "critical"], unknown: ["无法评估", "neutral"],
    }[chg];
    return m ? `<span class="badge badge-${m[1]}">${m[0]}</span>` : "";
  }

  function emotionBrief(it) {
    if (!it) return "";
    const sc = scoreOf(it);
    return `${EMOJI[it.emotion] || "🗨️"} ${UI.esc(it.emotion)}
      <span class="badge badge-${LEVEL[it.emotion] || "neutral"}">${sc > 0 ? "+" : ""}${sc}</span>`;
  }

  function openAssistantModal(a) {
    UI.openModal({
      title: `◆ 助理回复 · ${UI.esc(a.assistant_name || "未识别")}`,
      content: `<div class="muted" style="margin-bottom:8px">第 ${a.turn_no} 轮 · ${a.timestamp ? UI.esc(fmtTs(a.timestamp)) : "无时间戳"}</div>
        <div style="background:var(--bg-1);padding:12px;border-radius:8px">${UI.esc(a.text || "")}</div>
        <div style="margin-top:12px">
          <div class="muted" style="margin-bottom:4px">回复前客户情绪</div>
          <div>${a.before ? emotionBrief(a.before) : '<span class="muted" style="font-size:12px">回复前无客户消息</span>'}</div>
          <div class="muted" style="margin:10px 0 4px">回复后客户情绪</div>
          <div>${a.after ? emotionBrief(a.after) : '<span class="muted" style="font-size:12px">回复后无客户消息</span>'}</div>
        </div>
        <div style="margin-top:12px">回复前后变化：${changeBadge(a.change)}
          ${a.change === "unknown" ? '<span class="muted" style="font-size:12px">（缺少前后客户情绪，无法评估）</span>' : ""}
        </div>`,
    });
  }

  function openTurningModal(tp) {
    UI.openModal({
      title: `▲ 情绪转折点 · 第 ${tp.turn_no} 轮`,
      content: `<div class="muted" style="margin-bottom:8px">${tp.timestamp ? UI.esc(fmtTs(tp.timestamp)) : "无时间戳"}</div>
        <div class="muted" style="margin-bottom:4px">客户原话</div>
        <div style="background:var(--bg-1);padding:12px;border-radius:8px">${UI.esc(tp.evidence || "")}</div>
        <div style="margin-top:12px">${emotionBrief({ emotion: tp.prev_emotion, emotion_score: tp.prev_score })}
          <span class="muted" style="margin:0 8px">→</span>
          ${emotionBrief({ emotion: tp.next_emotion, emotion_score: tp.next_score })}</div>
        <div style="margin-top:10px">变化：${changeBadge(tp.change)}</div>`,
    });
  }

  function openRiskModal(rp) {
    UI.openModal({
      title: `❗ 情绪风险点 · 第 ${rp.turn_no} 轮`,
      content: `<div class="muted" style="margin-bottom:8px">${rp.timestamp ? UI.esc(fmtTs(rp.timestamp)) : "无时间戳"} · 会话中最严重的负面情绪时刻</div>
        <div style="margin-bottom:8px">${EMOJI[rp.emotion] || ""} ${UI.esc(rp.emotion)}
          <span class="badge badge-critical">强度 ${rp.emotion_intensity}</span>
          <span class="badge badge-${LEVEL[rp.emotion] || "neutral"}">分值 ${rp.emotion_score > 0 ? "+" : ""}${rp.emotion_score}</span></div>
        <div class="muted" style="margin-bottom:4px">客户原话</div>
        <div style="background:var(--bg-1);padding:12px;border-radius:8px">${UI.esc(rp.evidence || "")}</div>`,
    });
  }

  function curveHtml(d) {
    const curve = d.curve;
    if (!curve) return "";
    const st = curve.stats || {};
    const emo = (it) => (it
      ? `<div style="font-size:18px;line-height:1.3">${EMOJI[it.emotion] || "🗨️"} ${UI.esc(it.emotion)}</div>
        <span class="badge badge-${LEVEL[it.emotion] || "neutral"}" style="margin-top:6px">分值 ${it.emotion_score > 0 ? "+" : ""}${it.emotion_score}</span>`
      : "—");
    const degradedNote = curve.degraded
      ? `<div class="alert alert-warning mt-8"><div>⚠️</div><div>旧数据未含时间戳/助理回复，曲线为降级显示（仅客户情绪折线，无时间轴与助理节点）；可重新生成情绪分析获得完整曲线。</div></div>`
      : "";
    return `<h3 style="margin:16px 0 8px">📈 客户情绪曲线</h3>
      <div id="emotion-curve-chart" style="width:100%;height:340px"></div>
      <div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:12px">
        ${statTile("初始情绪", emo(st.initial), "")}
        ${statTile("最低情绪", emo(st.lowest), "")}
        ${statTile("最终情绪", emo(st.final), "")}
        ${statTile("情绪改善次数", st.improved_count ?? 0, "")}
        ${statTile("情绪恶化次数", st.worsened_count ?? 0, "")}
        ${statTile("情绪转折次数", st.turning_count ?? 0, "")}
      </div>
      ${degradedNote}
      <p class="muted" style="font-size:12px">背景色带：绿=积极 · 橙=轻度负面 · 红=严重负面。◆ 助理回复（颜色=回复效果：绿改善 / 红恶化 / 灰无变化 / 蓝无法评估）· ▲ 情绪转折点（相邻客户消息情绪分值变化 ≥ 2）· ❗ 情绪风险点（最严重负面）。点击任一节点查看详情。</p>`;
  }

  function initCurve(container, d) {
    const chartEl = container.querySelector("#emotion-curve-chart");
    if (!chartEl || typeof echarts === "undefined") return;
    const curve = d.curve;
    if (!curve) return;
    // 客轮点直接使用后端按时间排序后的 points，避免重新按 turn_no 打乱时间轴。
    const fallbackTimeline = d.timeline || [];
    const cust = (curve.points || fallbackTimeline).map((point, index) => {
      const it = point.emotion ? point : fallbackTimeline.find((x) => x.turn_no === point.turn_no) || point;
      return {
        kind: "cust", sequence: point.sequence ?? index * 2, turn_no: point.turn_no,
        ts: point.timestamp ?? null, emotion: it.emotion, score: scoreOf(point),
        intensity: point.emotion_intensity ?? it.intensity,
      };
    });
    const asst = (curve.assistant_replies || []).map((a) => ({
      kind: "asst", sequence: a.sequence ?? (cust.length * 2 + a.turn_no), turn_no: a.turn_no, ts: a.timestamp,
      emotion: null, score: 0, assistant_name: a.assistant_name,
    }));
    const xs = [...cust, ...asst].sort((a, b) => a.sequence - b.sequence);
    const labels = xs.map((x) => (x.ts ? fmtTs(x.ts) : `第${x.turn_no}轮`));
    const lineData = xs.map((x) => (x.kind === "cust" ? x.score : null));
    // 助理回复节点：y = 回复时刻客户情绪水平（before 优先，无则 after，都无则 0），
    // 再 ±0.35 偏移避开客户点重合（正情绪偏上、负情绪偏下）；颜色按回复效果
    const asstPts = [];
    const assistantBySequence = new Map((curve.assistant_replies || []).map((a) => [a.sequence, a]));
    xs.forEach((x, i) => {
      if (x.kind !== "asst") return;
      const a = assistantBySequence.get(x.sequence)
        || (curve.assistant_replies || []).find((r) => r.turn_no === x.turn_no) || {};
      const base = a.before && a.before.emotion_score != null ? a.before.emotion_score
        : a.after && a.after.emotion_score != null ? a.after.emotion_score : 0;
      // 正情绪偏上、负情绪偏下，±0.35 避开客户点重合；接近绘图边界（+2/-4）时反向
      const y = base >= 1.5 ? base - 0.35 : base <= -3.5 ? base + 0.35
        : base >= 0 ? base + 0.35 : base - 0.35;
      const brief = (b) => (b ? `${b.emotion} ${b.emotion_score > 0 ? "+" : ""}${b.emotion_score}` : "无客户消息");
      asstPts.push({
        value: [i, y],
        _sequence: x.sequence,
        _turn: x.turn_no,
        _name: a.assistant_name || "未识别",
        _ts: a.timestamp ? fmtTs(a.timestamp) : "",
        _chg: a.change,
        _chgTxt: CHANGE_TXT[a.change] || "",
        _before: brief(a.before),
        _after: brief(a.after),
      });
    });
    // 标记点垂直偏移：默认在客户点上方 0.6；接近绘图区边界（+2/-4）时反向，避免出界
    const shiftY = (s) => (s >= 1.5 ? s - 0.6 : s + 0.6);
    const markTurns = new Set(); // 有 ▲/❗ 标记的客户轮次 → emoji 让位，避免标签叠加
    const markPts = [];
    (curve.turning_points || []).forEach((tp) => {
      const i = xs.findIndex((x) => x.turn_no === tp.turn_no);
      if (i >= 0) {
        markTurns.add(tp.turn_no);
        markPts.push({
          coord: [i, shiftY(tp.next_score)], name: "转折", _kind: "turning", ...tp,
          symbol: "triangle", symbolSize: 18,
          itemStyle: { color: CURVE_COLORS.turning },
          label: { show: true, formatter: "转折", position: "top", fontSize: 10, color: CURVE_COLORS.turning },
        });
      }
    });
    if (curve.risk_point) {
      const rp = curve.risk_point;
      const i = xs.findIndex((x) => x.turn_no === rp.turn_no);
      if (i >= 0) {
        markTurns.add(rp.turn_no);
        markPts.push({
          coord: [i, shiftY(rp.emotion_score)], name: "风险", _kind: "risk", ...rp,
          symbol: "pin", symbolSize: 20,
          itemStyle: { color: CURVE_COLORS.risk },
          label: { show: true, formatter: "风险", position: "top", fontSize: 10, color: CURVE_COLORS.riskLight },
        });
      }
    }

    if (curveChart) { curveChart.dispose(); curveChart = null; }
    curveChart = echarts.init(chartEl);
    // 情绪区带：y 轴横向分区（绿=积极 / 橙=轻度负面 / 红=严重负面），直观看出情绪落在哪个区间
    const bands = [
      { name: "积极", from: 0, to: 2, color: PALETTE.band.good, labelColor: PALETTE.band.goodLabel },
      { name: "轻度负面", from: -2, to: 0, color: PALETTE.band.mild, labelColor: PALETTE.band.mildLabel },
      { name: "严重负面", from: -4, to: -2, color: PALETTE.band.bad, labelColor: PALETTE.band.badLabel },
    ];
    const showEmoji = cust.length > 0 && cust.length <= 12; // 客户点多时隐藏 emoji 避免拥挤
    curveChart.setOption({
      backgroundColor: "transparent",
      animationDuration: 700,
      animationEasing: "cubicOut",
      textStyle: { color: CURVE_COLORS.text2, fontFamily: "-apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif" },
      legend: {
        top: 2, right: 8, icon: "roundRect", itemWidth: 16, itemHeight: 8,
        textStyle: { color: CURVE_COLORS.text2, fontSize: 11 },
        data: ["客户情绪", "助理回复"],
      },
      tooltip: {
        backgroundColor: PALETTE.tooltip.bg, borderColor: PALETTE.tooltip.border,
        textStyle: { color: PALETTE.tooltip.text, fontSize: 12 },
        extraCssText: `box-shadow: ${PALETTE.tooltip.shadow}; border-radius: 6px;`,
        formatter: (p) => {
          if (p.componentSubType === "markPoint") {
            const d0 = p.data;
            if (d0._kind === "risk") return `❗ 情绪风险点<br/>${d0.emotion}（强度 ${d0.emotion_intensity}）<br/>分值 ${d0.emotion_score}`;
            return `▲ 情绪转折点<br/>${d0.prev_emotion} → ${d0.next_emotion}<br/>分值 ${d0.prev_score} → ${d0.next_score}`;
          }
          if (p.seriesType === "scatter") {
            return `◆ 助理回复 · ${UI.esc(p.data._name)}（效果：${p.data._chgTxt}）<br/>${p.data._ts || "无时间戳"}<br/>回复前 ${p.data._before} → 回复后 ${p.data._after}`;
          }
          const x0 = xs[p.dataIndex];
          if (!x0 || x0.kind !== "cust") return "";
          const it = (d.timeline || []).find((t) => t.turn_no === x0.turn_no);
          return `${fmtTs(x0.ts) || `第${x0.turn_no}轮`}<br/>${EMOJI[x0.emotion] || ""} ${x0.emotion}（强度 ${x0.intensity}）<br/>分值 ${x0.score}${it && it.evidence ? `<br/><span style="color:${PALETTE.text3}">${UI.esc(it.evidence.slice(0, 60))}</span>` : ""}`;
        },
      },
      grid: { left: 44, right: 66, top: 34, bottom: 42 },
      xAxis: {
        type: "category", data: labels, boundaryGap: false,
        axisLine: { lineStyle: { color: CURVE_COLORS.axis } },
        axisLabel: { color: CURVE_COLORS.text3, fontSize: 10, rotate: 40, interval: Math.max(0, Math.floor(labels.length / 12) - 1) },
        axisTick: { show: false },
      },
      yAxis: {
        type: "value", min: -4, max: 2, interval: 1,
        axisLabel: { color: CURVE_COLORS.text3, fontSize: 11, formatter: (v) => (v > 0 ? `+${v}` : String(v)) },
        splitLine: { lineStyle: { color: CURVE_COLORS.grid } },
      },
      series: [
        {
          // connectNulls: 跨过助理回复位置连成完整连续曲线（助轮处无线段点，由 ◆ 节点标记时刻）
          type: "line", name: "客户情绪", data: lineData, connectNulls: true,
          symbol: "circle", symbolSize: 10,
          lineStyle: { color: CURVE_COLORS.assistant, width: 2 },
          itemStyle: { color: (p) => (p.value == null ? CURVE_COLORS.assistant : scoreColor(p.value)), borderColor: CURVE_COLORS.dotBorder, borderWidth: 1.5 },
          // emoji 标签（仅客轮点；点多自动隐藏；有 ▲/❗ 标记的轮次让位给标记标签，避免叠加）
          label: {
            show: showEmoji, position: "top", fontSize: 13,
            formatter: (p) => {
              const x0 = xs[p.dataIndex];
              return x0 && x0.kind === "cust" && !markTurns.has(x0.turn_no) && EMOJI[x0.emotion] ? EMOJI[x0.emotion] : "";
            },
          },
          areaStyle: {
            // 蓝色氛围渐变：越靠近曲线顶部越明显
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: PALETTE.areaTop },
              { offset: 1, color: PALETTE.areaBottom },
            ]),
          },
          markArea: {
            silent: true,
            data: bands.map((b) => [
              {
                name: b.name, yAxis: b.from,
                itemStyle: { color: b.color },
                label: { show: true, position: "right", color: b.labelColor, fontSize: 10, distance: 6 },
              },
              { yAxis: b.to },
            ]),
          },
          markLine: {
            silent: true, symbol: "none",
            label: { formatter: "中性线", color: CURVE_COLORS.text3, fontSize: 10, position: "insideEndTop" },
            lineStyle: { color: CURVE_COLORS.axis, type: "dashed", width: 1 },
            data: [{ yAxis: 0 }],
          },
          markPoint: { data: markPts },
        },
        {
          type: "scatter", name: "助理回复", data: asstPts,
          symbol: "diamond", symbolSize: 14,
          itemStyle: { color: (p) => CHANGE_COLOR[p.data._chg] || CURVE_COLORS.assistant, borderColor: CURVE_COLORS.dotBorder, borderWidth: 1.5 },
          // 助理名标签（超 5 字省略；放节点下方，与客户点上方 emoji 上下错开不叠加）
          label: {
            show: true, position: "bottom", fontSize: 10, distance: 2,
            color: (p) => CHANGE_COLOR[p.data._chg] || CURVE_COLORS.text2,
            formatter: (p) => (p.data._name.length > 5 ? `${p.data._name.slice(0, 5)}…` : p.data._name),
          },
          // 垂直虚线：节点 → 中性基线，标记介入时刻
          markLine: {
            silent: true, symbol: "none",
            lineStyle: { color: PALETTE.areaLine, type: "dashed", width: 1 },
            label: { show: false },
            data: asstPts.map((pt) => [{ xAxis: pt.value[0], yAxis: pt.value[1] }, { xAxis: pt.value[0], yAxis: 0 }]),
          },
        },
      ],
    });
    curveChart.on("click", (p) => {
      if (p.componentSubType === "markPoint") {
        if (p.data._kind === "risk") openRiskModal(p.data);
        else openTurningModal(p.data);
      } else if (p.seriesType === "scatter") {
        const a = assistantBySequence.get(p.data._sequence)
          || (curve.assistant_replies || []).find((x) => x.turn_no === p.data._turn);
        if (a) openAssistantModal(a);
      } else if (p.seriesType === "line") {
        const x0 = xs[p.dataIndex];
        if (!x0 || x0.kind !== "cust") return;
        const it = (d.timeline || []).find((t) => t.turn_no === x0.turn_no);
        if (!it) return;
        UI.openModal({
          title: `第 ${it.turn_no} 轮 · ${it.emotion}（强度 ${it.intensity} / 置信 ${it.confidence?.toFixed(2)}）`,
          content: `<div class="muted" style="margin-bottom:8px">客户原话证据</div>
            <div style="background:var(--bg-1);padding:12px;border-radius:8px">${UI.esc(evidenceOf(it))}</div>`,
        });
      }
    });
  }

  if (typeof window !== "undefined") {
    window.addEventListener("resize", () => { if (curveChart) curveChart.resize(); });
  }

  function isLowConf(item) {
    return !!(item && (item.synthesized || item.confidence < CONF_THRESHOLD));
  }

  function lowBadge(item) {
    return isLowConf(item) ? '<span class="badge badge-warning">低置信度</span>' : "";
  }

  function evidenceOf(item) {
    const ev = item.evidence || "";
    const syn = item.synthesized ? "（LLM 未输出/无文本，系统按无明显情绪合成中性）" : "";
    const adj = item.evidence_adjusted ? "（未匹配到可引用的原话，已展示本条完整内容）" : "";
    return `${ev}${syn}${adj}`;
  }

  function loadingHtml(text) {
    return `<div class="empty"><div class="spinner" style="margin-bottom:12px"></div>${text}</div>`;
  }

  function notGeneratedHtml(reportId) {
    return `<div class="empty">
      <div class="empty-icon" style="font-size:32px">📊</div>
      <div>该会话尚未生成客户情绪分析</div>
      <div class="muted" style="font-size:12px">将逐条识别客户消息情绪（8 类）与强度，并统计情绪时间线、变化次数与各助理情绪服务效果</div>
      <div class="mt-16"><button class="btn btn-primary" id="btn-emotion-analyze">生成情绪分析</button></div>
    </div>`;
  }

  function unsupportedHtml(message) {
    return `<div class="alert alert-warning mt-8"><div>⚠️</div>
      <div><b>情绪分析不可用</b><div class="mt-4">${UI.esc(message || "")}</div></div></div>`;
  }

  function errorHtml(message) {
    return `<div class="alert alert-warning mt-8"><div>⚠️</div>
      <div><b>情绪分析加载失败</b><div class="mt-4">${UI.esc(message || "")}</div>
      <button class="btn btn-sm btn-ghost mt-8" id="btn-emotion-retry">重试</button></div></div>`;
  }

  function statTile(label, value, extra) {
    return `<div class="stat-tile">
      <div class="stat-value">${value}</div>
      <div class="stat-label">${label}</div>
      ${extra ? `<div class="stat-extra">${extra}</div>` : ""}
    </div>`;
  }

  function timelineHtml(timeline) {
    if (!timeline || !timeline.length) return "";
    return `<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">
      ${timeline.map((it, idx) => {
        const level = LEVEL[it.emotion] || "neutral";
        const chip = `<span class="chip" data-emotion-turn="${it.turn_no}"
          title="第 ${it.turn_no} 轮 · ${UI.esc(it.evidence || "")}"
          style="cursor:pointer">${EMOJI[it.emotion] || "🗨️"} ${UI.esc(it.emotion)}
          <span class="badge badge-${level}">${it.intensity}</span>${lowBadge(it)}</span>`;
        const arrow = idx === 0 || !it.change ? "" : `<span class="muted" style="font-size:12px">${CHANGE_LABEL[it.change]?.text || "→"}</span>`;
        return `${arrow}${chip}`;
      }).join("")}
    </div>
    <p class="muted" style="font-size:12px">点击任一情绪查看客户原话证据；数字为情绪强度（0-5）。</p>`;
  }

  function perAssistantHtml(perAssistant) {
    if (!perAssistant || !perAssistant.length) {
      return `<p class="muted" style="font-size:12px">无有效的前后客户情绪对，无法评估助理情绪服务效果</p>`;
    }
    return `<table class="table">
      <thead><tr><th>助理</th><th>负面情绪</th><th>情绪改善</th><th>情绪恶化</th><th>无明显变化</th><th>情绪改善率</th></tr></thead>
      <tbody>
        ${perAssistant.map((p) => {
          const rate = p.improve_rate == null
            ? '<span class="muted">—</span>'
            : `<span class="num">${Math.round(p.improve_rate * 100)}%</span> <span class="muted" style="font-size:12px">(${p.improved}/${p.evaluable_pairs})</span>`;
          return `<tr>
            <td><b>${UI.esc(p.assistant_name || "未识别")}</b></td>
            <td class="num">${p.negative_count}</td>
            <td class="num">${p.improved}</td>
            <td class="num">${p.worsened}</td>
            <td class="num">${p.unchanged}</td>
            <td>${rate}</td>
          </tr>`;
        }).join("")}
      </tbody>
    </table>
    <p class="muted" style="font-size:12px">改善率 = 该助理回复后的客户情绪改善对数 / 其可评估前后情绪对总数；仅计入存在明确前后客户情绪且该助理有回复的轮次。</p>`;
  }

  function dataHtml(d) {
    const cur = d.current || {};
    const ch = d.changes || {};
    const curLevel = LEVEL[cur.emotion] || "neutral";
    const notJudgedNote = (ch.not_judged || 0) > 0
      ? `<p class="muted" style="font-size:12px">其中 ${ch.not_judged} 对相邻客户消息之间无助理回复，无法判断助理效果（不计入改善率）。</p>`
      : "";
    const degradedNote = d.degraded
      ? `<div class="alert alert-warning mt-8"><div>⚠️</div><div>${UI.esc(d.warning || "部分数据为降级重建，助理归属可能不完整")}</div></div>`
      : "";
    return `${degradedNote}
      <div style="display:flex;gap:16px;flex-wrap:wrap">
        ${statTile("当前情绪", `${EMOJI[cur.emotion] || ""} ${UI.esc(cur.emotion || "—")}`, "")}
        ${statTile("情绪强度", cur.intensity ?? "—", "")}
        ${statTile("置信度", cur.confidence != null ? cur.confidence.toFixed(2) : "—", lowBadge(cur) || "")}
        ${statTile("情绪变化次数", ch.total ?? 0, "")}
        ${statTile("改善", ch.improved ?? 0, "")}
        ${statTile("恶化", ch.worsened ?? 0, "")}
        ${statTile("负面情绪", d.negative_count ?? 0, "")}
        ${statTile("低置信度", d.low_confidence_count ?? 0, "")}
      </div>
      <h3 style="margin:16px 0 8px">情绪时间线（按消息顺序）</h3>
      ${timelineHtml(d.timeline)}
      ${curveHtml(d)}
      <h3 style="margin:16px 0 8px">主要情绪触发原因</h3>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        ${(d.main_triggers || []).length
          ? d.main_triggers.map((t) => `<span class="tag">${UI.esc(t.trigger)} ×${t.count}</span>`).join("")
          : '<span class="muted">无明显可归纳的触发原因</span>'}
      </div>
      <h3 style="margin:16px 0 8px">各助理情绪服务效果</h3>
      ${perAssistantHtml(d.per_assistant)}
      ${notJudgedNote}`;
  }

  function bindEvidenceModal(container) {
    container.querySelectorAll("[data-emotion-turn]").forEach((el) => {
      el.addEventListener("click", () => {
        const turn = Number(el.dataset.emotionTurn);
        const item = (window.__emotionData?.timeline || []).find((it) => it.turn_no === turn);
        if (!item) return;
        UI.openModal({
          title: `第 ${turn} 轮 · ${item.emotion}（强度 ${item.intensity} / 置信 ${item.confidence?.toFixed(2)}）`,
          content: `<div class="muted" style="margin-bottom:8px">客户原话证据</div>
            <div style="background:var(--bg-1);padding:12px;border-radius:8px">${UI.esc(evidenceOf(item))}</div>`,
        });
      });
    });
  }

  function render(container, reportId, err, data) {
    let html;
    if (data) {
      window.__emotionData = data;
      html = dataHtml(data);
    } else if (err && err.code === "emotion_not_found") {
      html = notGeneratedHtml(reportId);
    } else if (err && (err.code === "emotion_unsupported" || err.code === "not_found")) {
      html = unsupportedHtml(err.message);
    } else if (err) {
      html = errorHtml(err.message);
    } else {
      html = loadingHtml("正在加载客户情绪…");
    }
    container.innerHTML = html;
    bindEvidenceModal(container);
    if (data) initCurve(container, data);
    else if (curveChart) { curveChart.dispose(); curveChart = null; }

    const genBtn = container.querySelector("#btn-emotion-analyze");
    if (genBtn) {
      genBtn.addEventListener("click", async () => {
        genBtn.disabled = true;
        container.innerHTML = loadingHtml("正在分析客户情绪（逐条识别 8 类情绪与强度），可能需要数十秒…");
        try {
          const d = await API.post("/api/emotion/analyze", { inspection_id: reportId });
          render(container, reportId, null, d);
        } catch (e) {
          container.innerHTML = errorHtml(e.message || "分析失败");
        }
      });
    }
    const retryBtn = container.querySelector("#btn-emotion-retry");
    if (retryBtn) {
      retryBtn.addEventListener("click", () => load(container, reportId));
    }
  }

  function load(container, reportId) {
    container.innerHTML = loadingHtml("正在加载客户情绪…");
    API.get(`/api/emotion/inspection/${reportId}`)
      .then((d) => render(container, reportId, null, d))
      .catch((err) => render(container, reportId, err, null));
  }

  // 报告页入口：app 容器 + 报告对象（仅用 r.id）
  window.EmotionCard.init = function (app, report) {
    let box = app.querySelector("#emotion-card");
    if (!box) {
      box = document.createElement("div");
      box.id = "emotion-card";
      box.className = "card mt-16";
      box.innerHTML = `<div class="card-title">客户情绪分析</div><div id="emotion-body"></div>`;
      app.appendChild(box);
    }
    const body = box.querySelector("#emotion-body") || box;
    load(body, report.id);
  };
})();
