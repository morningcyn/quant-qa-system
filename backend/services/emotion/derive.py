# 客户情绪确定性派生：情绪变化 / 助理效果归属 / 会话摘要
# 全部为纯规则计算（可复现、不依赖 LLM），LLM 只负责逐条情绪标注。
from typing import Any

# 负面情绪集合（积极/认可、中性 之外的 6 类）
NEGATIVE_EMOTIONS = {"担忧", "怀疑", "失望", "焦虑", "不满", "愤怒"}

# 情绪负面程度排序（用于 emotion_change 方向判定）：
# 积极/认可(-2) < 中性(0) < 担忧(1) < 怀疑(2) < 失望(3) < 焦虑(4) < 不满(5) < 愤怒(6)
# 依据：担忧是最轻负面（面向未来、伴随求助、非对抗）；怀疑/失望是认知评价型；
# 焦虑是高唤醒不安；不满是指向服务的抱怨；愤怒是唤醒与对抗性峰值。
# 这是固定确定性配置，改变它只影响派生统计，不涉及 LLM。
EMOTION_RANK = {
    "积极/认可": -2,
    "中性": 0,
    "担忧": 1,
    "怀疑": 2,
    "失望": 3,
    "焦虑": 4,
    "不满": 5,
    "愤怒": 6,
}

# 情绪分值（emotion_score，仅用于情绪曲线趋势绘制，与 EMOTION_RANK 完全独立）：
# 积极/认可=+2, 中性=0, 担忧/怀疑=-1, 失望/焦虑=-2, 不满=-3, 愤怒=-4
# 这是确定性映射表（emotion → score），由程序在落库时补算，不进入 LLM 契约。
# 注意：它是绘图用的粗粒度刻度，不是真实心理测量值。
EMOTION_SCORE = {
    "积极/认可": 2,
    "中性": 0,
    "担忧": -1,
    "怀疑": -1,
    "失望": -2,
    "焦虑": -2,
    "不满": -3,
    "愤怒": -4,
}

# confidence 低于该值 → 前端标记「低置信度」（synthesized 合成项恒为 0.0，自然落入）
LOW_CONFIDENCE_THRESHOLD = 0.5

# 情绪颜色（前端 chip 用，主题色系）
EMOTION_LEVEL = {
    "积极/认可": "good",
    "中性": "neutral",
    "担忧": "warning",
    "怀疑": "warning",
    "失望": "critical",
    "焦虑": "critical",
    "不满": "critical",
    "愤怒": "critical",
}

# 派生摘要结构（summary_json 落库）：
# {
#   "current": item,                                # 末条客户消息情绪
#   "timeline": [{**item, "change": "improved"|"unchanged"|"worsened"|None}],  # 首条 change=None
#   "changes": {"total", "improved", "worsened", "unchanged", "not_judged"},
#   "negative_count": int,
#   "low_confidence_count": int,
#   "main_triggers": [{"trigger": str, "count": int}],   # 排除 未知/其他，降序 top3
#   "per_assistant": [{assistant_id, assistant_name, negative_count,
#                      improved, worsened, unchanged, evaluable_pairs, improve_rate}],
# }


def emotion_change(a: dict, b: dict) -> str:
    """相邻客户消息对 (a→b) 的情绪变化：先比负面程度，同程度再比强度，全同则不变。
    rank 越大越负面：b 比 a 负面 → worsened；b 比 a 正面 → improved。"""
    ra, rb = EMOTION_RANK[a["emotion"]], EMOTION_RANK[b["emotion"]]
    if ra > rb:
        return "improved"
    if ra < rb:
        return "worsened"
    if a["intensity"] > b["intensity"]:
        return "improved"
    if a["intensity"] < b["intensity"]:
        return "worsened"
    return "unchanged"


def _assistant_key(m) -> tuple[Any, str]:
    """助理标识：优先员工 id，未匹配员工（降级重建路径）退化为规范名。"""
    return (m.assistant_id, m.canonical_name) if m.assistant_id else (None, m.canonical_name or "未识别")


def build_summary(messages: list, items: list[dict]) -> dict:
    """全量消息（MultiMessage 列表，客/助轮次有序）+ 逐条情绪 items → 派生摘要。

    items 已按 turn_no 对齐（含 synthesized 合成项，保证每条客轮都有情绪）。
    """
    items_by_turn = {it["turn_no"]: it for it in items}
    # 客轮（时间序）与助轮列表
    cust = [m for m in messages if getattr(m, "role", None) == "客"]
    asst = [m for m in messages if getattr(m, "role", None) == "助"]
    asst.sort(key=lambda m: m.turn_no)

    timeline: list[dict] = []
    changes = {"total": 0, "improved": 0, "worsened": 0, "unchanged": 0, "not_judged": 0}
    # per-assistant 统计桶
    buckets: dict[tuple, dict] = {}

    def bucket(key: tuple) -> dict:
        if key not in buckets:
            buckets[key] = {
                "assistant_id": key[0],
                "assistant_name": key[1],
                "negative_count": 0,
                "improved": 0,
                "worsened": 0,
                "unchanged": 0,
                "evaluable_pairs": 0,
            }
        return buckets[key]

    # 客轮负面计数归属：客轮 k 前最近一条助轮属哪个助理
    for m in cust:
        it = items_by_turn.get(m.turn_no)
        if not it:
            continue
        prev_asst = [a for a in asst if a.turn_no < m.turn_no]
        if prev_asst and it["emotion"] in NEGATIVE_EMOTIONS:
            bucket(_assistant_key(prev_asst[-1]))["negative_count"] += 1

    # 相邻客户消息对：时间线恒算全部对；助理效果归属「中间最后一条助轮」
    for k in range(len(cust)):
        m = cust[k]
        it = items_by_turn.get(m.turn_no)
        if not it:
            continue
        item = dict(it)
        if k == 0:
            item["change"] = None
            timeline.append(item)
            continue
        prev = cust[k - 1]
        prev_it = items_by_turn.get(prev.turn_no)
        if not prev_it:
            item["change"] = None
            timeline.append(item)
            continue
        between = [a for a in asst if prev.turn_no < a.turn_no < m.turn_no]
        chg = emotion_change(prev_it, it)
        changes["total"] += 1
        changes[chg] += 1
        item["change"] = chg
        if between:
            key = _assistant_key(between[-1])
            b = bucket(key)
            b[chg] += 1
            b["evaluable_pairs"] += 1
        else:
            changes["not_judged"] += 1
        timeline.append(item)

    # 首条客轮无前置对 → 时间线基准（change=None 已在上面处理）
    per_assistant = []
    for b in buckets.values():
        per_assistant.append(
            {
                **b,
                "improve_rate": (
                    round(b["improved"] / b["evaluable_pairs"], 3) if b["evaluable_pairs"] else None
                ),
            }
        )
    per_assistant.sort(key=lambda x: (x["assistant_name"] or "").lower())

    # 触发原因 top3（排除 未知/其他；全为排除项 → 空表）
    trig: dict[str, int] = {}
    for it in items:
        t = it.get("trigger")
        if t and t not in ("未知", "其他"):
            trig[t] = trig.get(t, 0) + 1
    main_triggers = [
        {"trigger": t, "count": c} for t, c in sorted(trig.items(), key=lambda kv: -kv[1])[:3]
    ]

    return {
        "current": timeline[-1] if timeline else None,
        "timeline": timeline,
        "changes": changes,
        "negative_count": sum(b["negative_count"] for b in buckets.values()),
        "low_confidence_count": sum(
            1 for it in items if it.get("synthesized") or it.get("confidence", 1.0) < LOW_CONFIDENCE_THRESHOLD
        ),
        "main_triggers": main_triggers,
        "per_assistant": per_assistant,
    }


# ---------- 客户情绪曲线（纯派生，零 LLM） ----------
# curve 块结构（summary_json["curve"]，由已落库的分析结果确定性计算）：
# {
#   "assistant_replies": [{assistant_id, assistant_name, timestamp, text,
#                          before: {turn_no, emotion, emotion_score} | null,
#                          after: {...} | null,
#                          change: improved|unchanged|worsened|unknown}],  # 回复在会话头/尾时 unknown
#   "turning_points": [{turn_no, timestamp, evidence, prev_emotion, next_emotion,
#                       prev_score, next_score, change}],                  # 相邻客户对 |Δscore| ≥ 2
#   "risk_point": {turn_no, timestamp, emotion, emotion_score, emotion_intensity, evidence} | null,
#   "stats": {initial, lowest, final: {turn_no, emotion, emotion_score},
#             improved_count, worsened_count, turning_count},
#   "degraded": bool,   # 快照缺时间戳或助轮 → 曲线降级显示
# }


def _score(it: dict) -> int:
    """情绪分值：优先 item 已带 emotion_score（新数据），旧数据按情绪补算。"""
    s = it.get("emotion_score")
    return s if s is not None else EMOTION_SCORE.get(it.get("emotion"), 0)


def build_curve(messages_rows: list[dict], items: list[dict]) -> dict:
    """由落库快照 + 逐条情绪 → 情绪曲线（助理节点 / 转折点 / 风险点 / 统计）。

    messages_rows 兼容新旧快照格式：
      新：{turn_no, role, speaker, canonical_name, text, timestamp, assistant_id}（客+助全量）
      旧：{turn_no, speaker, text}（仅客户消息）→ 无时间戳/无助理节点，degraded=true
    items 为逐条情绪（旧行可能缺 emotion_score，按 EMOTION_SCORE 补算）。
    """
    rows = sorted(messages_rows, key=lambda r: r.get("turn_no", 0))
    items_by_turn = {it["turn_no"]: it for it in items}
    has_ts = any(r.get("timestamp") for r in rows)
    has_asst = any(r.get("role") == "助" for r in rows)
    degraded = not (has_ts and has_asst)

    def role_of(r: dict) -> str:
        role = r.get("role")
        if role:
            return role
        # 旧快照（无 role 键）只存过客户消息（speaker 可能是客户名/脱敏名）→ 一律按客轮
        return "客"

    cust_rows = [r for r in rows if role_of(r) == "客"]
    asst_rows = [r for r in rows if role_of(r) == "助"]

    def brief(it: dict) -> dict:
        return {"turn_no": it["turn_no"], "emotion": it["emotion"], "emotion_score": _score(it)}

    # 1) 助理回复事件节点：before=回复前最后一条客轮情绪，after=回复后第一条客轮情绪
    assistant_replies: list[dict] = []
    if has_asst:
        for r in asst_rows:
            before = after = None
            for c in cust_rows:
                if c["turn_no"] >= r["turn_no"]:
                    break
                it = items_by_turn.get(c["turn_no"])
                if it:
                    before = brief(it)
            for c in cust_rows:
                if c["turn_no"] > r["turn_no"]:
                    it = items_by_turn.get(c["turn_no"])
                    if it:
                        after = brief(it)
                    break
            change = "unknown"
            if before and after:
                change = emotion_change(items_by_turn[before["turn_no"]], items_by_turn[after["turn_no"]])
            assistant_replies.append(
                {
                    "turn_no": r.get("turn_no"),
                    "assistant_id": r.get("assistant_id"),
                    "assistant_name": r.get("canonical_name") or r.get("speaker") or "未识别",
                    "timestamp": r.get("timestamp") if has_ts else None,
                    "text": r.get("text", ""),
                    "before": before,
                    "after": after,
                    "change": change,
                }
            )

    # 2) 情绪转折点：相邻客户消息对 |Δscore| ≥ 2
    turning_points: list[dict] = []
    for k in range(1, len(cust_rows)):
        prev_it = items_by_turn.get(cust_rows[k - 1]["turn_no"])
        cur_it = items_by_turn.get(cust_rows[k]["turn_no"])
        if not prev_it or not cur_it:
            continue
        ps, cs = _score(prev_it), _score(cur_it)
        if abs(cs - ps) >= 2:
            turning_points.append(
                {
                    "turn_no": cur_it["turn_no"],
                    "timestamp": cust_rows[k].get("timestamp") if has_ts else None,
                    "evidence": cur_it.get("evidence", ""),
                    "prev_emotion": prev_it["emotion"],
                    "next_emotion": cur_it["emotion"],
                    "prev_score": ps,
                    "next_score": cs,
                    "change": emotion_change(prev_it, cur_it),
                }
            )

    # 3) 情绪风险点：最严重的负面客轮（score 最小；并列取强度大者，再并列取先出现）
    risk_point: dict | None = None
    negatives = [r for r in cust_rows if r["turn_no"] in items_by_turn and _score(items_by_turn[r["turn_no"]]) < 0]
    if negatives:
        best = min(
            negatives,
            key=lambda r: (
                _score(items_by_turn[r["turn_no"]]),
                -items_by_turn[r["turn_no"]].get("intensity", 0),
                r["turn_no"],
            ),
        )
        it = items_by_turn[best["turn_no"]]
        risk_point = {
            "turn_no": best["turn_no"],
            "timestamp": best.get("timestamp") if has_ts else None,
            "emotion": it["emotion"],
            "emotion_score": _score(it),
            "emotion_intensity": it.get("intensity", 0),
            "evidence": it.get("evidence", ""),
        }

    # 4) 统计：初始/最低/最终情绪 + 相邻客户对改善/恶化次数 + 转折次数
    cust_items = [items_by_turn[r["turn_no"]] for r in cust_rows if r["turn_no"] in items_by_turn]
    stats = {
        "initial": None,
        "lowest": None,
        "final": None,
        "improved_count": 0,
        "worsened_count": 0,
        "turning_count": len(turning_points),
    }
    if cust_items:
        stats["initial"] = brief(cust_items[0])
        stats["final"] = brief(cust_items[-1])
        lowest = min(cust_items, key=lambda it: (_score(it), -it.get("intensity", 0), it["turn_no"]))
        stats["lowest"] = brief(lowest)
        for a, b in zip(cust_items, cust_items[1:]):
            chg = emotion_change(a, b)
            if chg == "improved":
                stats["improved_count"] += 1
            elif chg == "worsened":
                stats["worsened_count"] += 1

    # 5) 客轮点时间序列（前端折线 x 轴：时间序 + 分值，助轮位置由前端与 assistant_replies 合并）
    points = [
        {
            "turn_no": r["turn_no"],
            "timestamp": r.get("timestamp") if has_ts else None,
            "emotion": items_by_turn[r["turn_no"]]["emotion"],
            "emotion_score": _score(items_by_turn[r["turn_no"]]),
            "emotion_intensity": items_by_turn[r["turn_no"]].get("intensity", 0),
        }
        for r in cust_rows
        if r["turn_no"] in items_by_turn
    ]

    return {
        "assistant_replies": assistant_replies,
        "turning_points": turning_points,
        "risk_point": risk_point,
        "stats": stats,
        "points": points,
        "degraded": degraded,
    }
