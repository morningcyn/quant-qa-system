// ECharts 暗色封装：初始化、雷达图、趋势折线、Top3 条形
(function () {
  "use strict";

  const COLORS = {
    text: "#ffffff", text2: "#c3c2b7", text3: "#898781",
    grid: "#2c2c2a", axis: "#383835",
    dBlue: "#3987e5", sTeal: "#199e70",
    good: "#0ca30c", warning: "#fab219", critical: "#d03b3b",
  };

  function baseOption() {
    return {
      backgroundColor: "transparent",
      textStyle: { color: COLORS.text2, fontFamily: "-apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif" },
    };
  }

  function tooltip() {
    return {
      trigger: "item",
      backgroundColor: "#21211f",
      borderColor: "#383835",
      textStyle: { color: "#ffffff", fontSize: 12 },
      extraCssText: "box-shadow: 0 6px 24px rgba(0,0,0,0.45); border-radius: 6px;",
    };
  }

  // 雷达图：indicators [{name, max}]，value {name, values:[{label, value}], color}
  function renderRadar(el, indicators, series) {
    const chart = echarts.init(el);
    const ind = indicators.map((i) => ({ name: i.name, max: i.max }));
    const option = Object.assign(baseOption(), {
      tooltip: tooltip(),
      radar: {
        indicator: ind,
        radius: "62%",
        splitNumber: 4,
        axisName: { color: COLORS.text2, fontSize: 12 },
        splitLine: { lineStyle: { color: [COLORS.grid] } },
        splitArea: { areaStyle: { color: ["transparent", "rgba(255,255,255,0.02)"] } },
        axisLine: { lineStyle: { color: COLORS.axis } },
      },
      series: series.map((s) => ({
        type: "radar",
        data: [{
          value: s.values.map((v) => v.value),
          name: s.name,
          areaStyle: { color: s.color, opacity: 0.22 },
          lineStyle: { color: s.color, width: 2 },
          itemStyle: { color: s.color },
        }],
        symbol: "circle",
        symbolSize: 5,
      })),
    });
    chart.setOption(option);
    window.addEventListener("resize", () => chart.resize());
    return chart;
  }

  // 趋势折线：points [{date, avg_score|null, count}]，threshold 画黄灯线
  function renderTrend(el, points, threshold) {
    const chart = echarts.init(el);
    const dates = points.map((p) => p.date.slice(5));
    const values = points.map((p) => (p.avg_score === null ? null : p.avg_score));
    const option = Object.assign(baseOption(), {
      tooltip: Object.assign(tooltip(), {
        trigger: "axis",
        axisPointer: { type: "cross", label: { backgroundColor: "#383835" } },
        formatter: (params) => {
          const p = points[params[0].dataIndex];
          if (p.avg_score === null) return `${p.date}<br/>无质检记录`;
          return `${p.date}<br/>均分 <b>${p.avg_score}</b>（${p.count} 条）`;
        },
      }),
      grid: { left: 42, right: 20, top: 30, bottom: 30 },
      xAxis: {
        type: "category", data: dates, boundaryGap: false,
        axisLine: { lineStyle: { color: COLORS.axis } },
        axisLabel: { color: COLORS.text3, fontSize: 11 },
        axisTick: { show: false },
      },
      yAxis: {
        type: "value", min: 0, max: 100,
        axisLabel: { color: COLORS.text3, fontSize: 11 },
        splitLine: { lineStyle: { color: COLORS.grid } },
      },
      series: [{
        type: "line", data: values, connectNulls: false,
        symbolSize: 8, showSymbol: true,
        lineStyle: { color: COLORS.dBlue, width: 2 },
        itemStyle: { color: (p) => (p.value === null ? COLORS.dBlue : p.value < threshold ? COLORS.critical : COLORS.dBlue) },
        markLine: {
          silent: true, symbol: "none",
          label: { formatter: `黄灯线 ${threshold}`, color: COLORS.warning, fontSize: 11, position: "insideEndTop" },
          lineStyle: { color: COLORS.warning, type: "dashed", width: 1 },
          data: [{ yAxis: threshold }],
        },
        areaStyle: { color: "rgba(57, 135, 229, 0.10)" },
      }],
    });
    chart.setOption(option);
    window.addEventListener("resize", () => chart.resize());
    return chart;
  }

  // Top3 横向条形：items [{name, loss_total, occurrence_count}]
  function renderTop3Bar(el, items, color) {
    const chart = echarts.init(el);
    const reversed = items.slice().reverse(); // ECharts 条形自下而上
    const option = Object.assign(baseOption(), {
      tooltip: Object.assign(tooltip(), {
        trigger: "axis", axisPointer: { type: "shadow" },
        formatter: (params) => {
          const it = items.find((x) => x.name === params[0].name) || {};
          return `${it.name}<br/>累计失分 <b>${it.loss_total}</b><br/>出现 ${it.occurrence_count} 次 · 平均 ${it.avg_score} 分`;
        },
      }),
      grid: { left: 8, right: 40, top: 6, bottom: 6, containLabel: true },
      xAxis: {
        type: "value",
        axisLabel: { color: COLORS.text3, fontSize: 11 },
        splitLine: { lineStyle: { color: COLORS.grid } },
      },
      yAxis: {
        type: "category", data: reversed.map((x) => x.name),
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { color: COLORS.text2, fontSize: 12 },
      },
      series: [{
        type: "bar", data: reversed.map((x) => x.loss_total),
        barWidth: 16,
        itemStyle: { color: color || COLORS.dBlue, borderRadius: [0, 4, 4, 0] },
        label: {
          show: true, position: "right", color: COLORS.text2, fontSize: 12,
          formatter: (p) => p.value,
        },
      }],
    });
    chart.setOption(option);
    window.addEventListener("resize", () => chart.resize());
    return chart;
  }

  window.Charts = { COLORS, renderRadar, renderTrend, renderTop3Bar };
})();
