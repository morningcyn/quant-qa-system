# 报告视图模型 + 统计聚合（30天趋势、Top3 失分——SQL 取行 + Python 后处理）
import json
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.db import repository
from backend.db.models import Inspection


def _loads(text: str | None, default):
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def build_report_view(session: Session, inspection: Inspection) -> dict:
    """质检报告视图模型（前端扁平化渲染用）。"""
    assistant = inspection.assistant
    detail = inspection.detail
    raw_dialogue = detail.raw_dialogue if detail else ""
    d_scores = _loads(detail.d_scores_json, {}) if detail else {}
    s_scores = _loads(detail.s_scores_json, {}) if detail else {}
    highlight = _loads(detail.highlight_dialogue_json, []) if detail else []
    suggestions = _loads(detail.suggestions_json, []) if detail else []
    snapshot = _loads(inspection.template_snapshot_json, {})
    na_dims = _loads(inspection.na_dims_json, [])
    # 折算前的有效得分（非 N/A 维度得分合计），供报告页展示"得分 X / 有效满分 Y → 折算总分"
    effective_score = None
    if na_dims:
        na_keys = {nd.get("key") for nd in na_dims}
        eff = 0
        for key, field in _D_FIELD_KEYS.items():
            if key in na_keys:
                continue
            s = (d_scores.get(field) or {}).get("score")
            if isinstance(s, (int, float)):
                eff += s
        for key, field in _S_FIELD_KEYS.items():
            if key in na_keys:
                continue
            s = (s_scores.get(field) or {}).get("score")
            if isinstance(s, (int, float)):
                eff += s
        effective_score = round(eff, 1)
    return {
        "id": inspection.id,
        "assistant_id": inspection.assistant_id,
        "assistant_name": assistant.name,
        "employee_no": assistant.employee_no,
        "session_title": inspection.session_title,
        "total_score": inspection.total_score,
        "is_red_alert": bool(inspection.is_red_alert),
        "red_alert_reasons": _loads(inspection.red_alert_reasons_json, []),
        "is_yellow_alert": inspection.is_yellow_alert,
        "yellow_alert_reasons": _loads(inspection.yellow_alert_reasons_json, []),
        "template_type": inspection.template_type,
        "template_name": snapshot.get("name", inspection.template_type),
        "template_snapshot": snapshot,
        "turn_count": inspection.turn_count,
        "customer_profile": inspection.customer_profile,
        "evaluatee": inspection.evaluatee,
        "na_dims": na_dims,
        "effective_max": inspection.effective_max,
        "effective_score": effective_score,
        "created_at": inspection.created_at.isoformat(sep=" ", timespec="seconds"),
        "d_scores": d_scores,
        "s_scores": s_scores,
        "highlight_dialogue": highlight,
        "improvement_suggestions": suggestions,
        "raw_dialogue": raw_dialogue,
        "parse_warnings": [],
    }


def trend_stats(session: Session, assistant_id: int, days: int = 30) -> dict:
    """近 N 天均分走势（Python 补零，无质检日为 null）+ 区间汇总。"""
    since = datetime.now() - timedelta(days=days - 1)
    rows = repository.inspection_score_rows_since(session, assistant_id, since)
    by_date: dict[str, list[int]] = {}
    yellow_count = 0
    for inspection, _detail in rows:
        key = inspection.created_at.strftime("%Y-%m-%d")
        by_date.setdefault(key, []).append(inspection.total_score)
        if inspection.is_yellow_alert:
            yellow_count += 1
    points = []
    scores = []
    for offset in range(days - 1, -1, -1):
        day = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
        values = by_date.get(day, [])
        avg = round(sum(values) / len(values), 1) if values else None
        points.append({"date": day, "avg_score": avg, "count": len(values)})
        if values:
            scores.extend(values)
    return {
        "days": days,
        "points": points,
        "total_avg": round(sum(scores) / len(scores), 1) if scores else None,
        "total_count": len(rows),
        "yellow_count": yellow_count,
        "latest_score": rows[-1][0].total_score if rows else None,
    }


# ---------- 优点（可借鉴）确定性推导（纯规则，不调 LLM） ----------

_GOOD_RATIO = 0.8    # 维度得分率 ≥80% 视为亮点
_MAX_STRENGTHS = 3   # 每位助理最多 3 条
_COMMENT_MAX = 40    # 维度 comment 截断长度
_DIM_AXIS_ORDER = ["d1", "d2", "d3", "d4", "s1", "s2", "s3"]  # 并列时固定轴序，保证确定性


def _pick_score(map_: dict, key: str) -> dict:
    """兼容短键（d1）与长键（d1_emotion_change）两种存储键（同 report.js pickScore）。"""
    if not map_:
        return {}
    if key in map_:
        return map_[key] or {}
    for k, v in map_.items():
        if k.startswith(key + "_"):
            return v or {}
    return {}


def _sub_value(raw) -> int | None:
    """子项 v2 为 {analysis, score} 对象，兼容旧格式纯数值。"""
    if isinstance(raw, dict):
        return raw.get("score")
    return raw


def derive_strengths(r: dict) -> list[str]:
    """从报告视图确定性推导「可借鉴」优点列表（不新增 LLM 调用）。

    高分维度（得分率 ≥80%）→ 亮点条目（可带 LLM 已写的维度 comment）；
    S 维度整体未达标时落到高分子项；D4 预判/掌控感动作是更可执行的亮点。
    候选不足 2 条时按确定性规则补足（合规健康/话术规范/持续接待）。
    """
    tpl = r.get("template_snapshot") or {}
    d_scores = r.get("d_scores") or {}
    s_scores = r.get("s_scores") or {}
    na_keys = {nd.get("key") for nd in r.get("na_dims") or []}
    candidates: list[tuple[float, int, int, str]] = []  # (ratio, max, axis_idx, text)

    for axis_idx, key in enumerate(_DIM_AXIS_ORDER):
        if key in na_keys:
            continue
        conf = (tpl.get("d") or {}).get(key) or (tpl.get("s") or {}).get(key) or {}
        data = _pick_score(d_scores if key.startswith("d") else s_scores, key)
        score = data.get("score")
        max_score = int(conf.get("max") or 0)
        if score is None or max_score <= 0:
            continue
        ratio = score / max_score
        name = conf.get("name") or key
        if ratio >= _GOOD_RATIO:
            if key == "d4":
                # 预判衍生问题 / 掌控感动作：比泛化措辞更可借鉴的动作型优点
                derived = int(data.get("derived_question") or 0)
                control = int(data.get("control_given") or 0)
                if derived >= 1 or control >= 1:
                    candidates.append(
                        (ratio, max_score, axis_idx, f"{name}：预判衍生问题 {derived} 个、掌控感动作 {control} 个")
                    )
                    continue
            text = f"{name}（{score}/{max_score} 分，得分率 {round(ratio * 100)}%）"
            comment = (data.get("comment") or "").strip()
            if comment:
                if len(comment) > _COMMENT_MAX:
                    comment = comment[:_COMMENT_MAX] + "…"
                text += f"：{comment}"
            candidates.append((ratio, max_score, axis_idx, text))
        else:
            # S 维度整体未达标：子项高分（如"针对性安慰"）是更可执行的动作优点
            subs = (tpl.get("s") or {}).get(key, {}).get("sub_items") or {}
            sub_data = data.get("sub_items") or {}
            for sub_key, sub_conf in subs.items():
                val = _sub_value(sub_data.get(sub_key))
                sub_max = int(sub_conf.get("max") or 0)
                if val is None or sub_max <= 0 or val / sub_max < _GOOD_RATIO:
                    continue
                candidates.append(
                    (val / sub_max, sub_max, axis_idx, f"{name}·{sub_conf.get('name', sub_key)}（{val}/{sub_max} 分）")
                )

    candidates.sort(key=lambda c: (-c[0], -c[1], c[2]))
    strengths = list(dict.fromkeys(c[3] for c in candidates))[:_MAX_STRENGTHS]

    # 补足：候选不足 2 条时按确定性规则补到 ≥2（与 dispatcher 降级风格一致）
    if len(strengths) < 2:
        fillers = []
        total = r.get("total_score")
        if not r.get("is_red_alert") and isinstance(total, (int, float)) and total >= 60:
            fillers.append(f"整体服务达标：无红灯违规，总分 {total} 分")
        if not (r.get("highlight_dialogue") or []):
            fillers.append("话术规范：本次会话未检出明显扣分话术")
        reply_count = r.get("reply_count") or r.get("turn_count") or 0
        if reply_count >= 3:
            fillers.append(f"全程持续接待：共 {reply_count} 次回复，服务衔接完整")
        if isinstance(total, (int, float)):
            fillers.append(f"整体表现合格（总分 {total} 分）")
        for f in fillers:
            if len(strengths) >= _MAX_STRENGTHS:
                break
            if f not in strengths:
                strengths.append(f)
    return strengths


def _item_loss_accumulate(
    key: str,
    name: str,
    score: int | None,
    max_score: int,
    agg: dict,
) -> None:
    # v2 兼容：S 子项可能是 {"analysis": ..., "score": int} 对象或旧格式 int
    if isinstance(score, dict):
        score = score.get("score")
    if score is None:
        return
    entry = agg.setdefault(
        key, {"key": key, "name": name, "loss_total": 0, "occurrence_count": 0, "score_sum": 0}
    )
    entry["loss_total"] += max(0, max_score - score)
    entry["occurrence_count"] += 1
    entry["score_sum"] += score


# 模板维度短键 → 评分 JSON 字段长键
_D_FIELD_KEYS = {
    "d1": "d1_emotion_change",
    "d2": "d2_profile_match",
    "d3": "d3_problem_match",
    "d4": "d4_expectation_exceed",
}
_S_FIELD_KEYS = {
    "s1": "s1_emotion_stabilize",
    "s2": "s2_problem_closure",
    "s3": "s3_professional_supply",
}


def top3_loss(session: Session, assistant_id: int, days: int = 30) -> dict:
    """历史失分最高 Top3（维度级 + S 端子项级）。失分 = 模板快照满分 − 实得分。"""
    since = datetime.now() - timedelta(days=days - 1)
    rows = repository.inspection_score_rows_since(session, assistant_id, since)
    dim_agg: dict = {}
    sub_agg: dict = {}
    for inspection, detail in rows:
        if detail is None:
            continue
        snapshot = _loads(inspection.template_snapshot_json, {})
        d_scores = _loads(detail.d_scores_json, {})
        s_scores = _loads(detail.s_scores_json, {})
        # N/A 豁免维度不参与失分统计（score 为 null 的维度天然不计，这里显式排除防异常数据）
        na_keys = {nd.get("key") for nd in _loads(inspection.na_dims_json, [])}
        for key, field in _D_FIELD_KEYS.items():
            if key in na_keys:
                continue
            conf = (snapshot.get("d") or {}).get(key, {})
            if not conf:
                continue
            score = (d_scores.get(field) or {}).get("score")
            _item_loss_accumulate(key, conf.get("name", key), score, int(conf.get("max", 0)), dim_agg)
        for key, field in _S_FIELD_KEYS.items():
            if key in na_keys:
                continue
            conf = (snapshot.get("s") or {}).get(key, {})
            if not conf:
                continue
            score = (s_scores.get(field) or {}).get("score")
            _item_loss_accumulate(key, conf.get("name", key), score, int(conf.get("max", 0)), dim_agg)
            subs = (s_scores.get(field) or {}).get("sub_items") or {}
            for sub_key, sub_conf in (conf.get("sub_items") or {}).items():
                _item_loss_accumulate(
                    f"{key}.{sub_key}",
                    f"{conf.get('name', key)}·{sub_conf.get('name', sub_key)}",
                    subs.get(sub_key),
                    int(sub_conf.get("max", 0)),
                    sub_agg,
                )
    return {
        "days": days,
        "dimensions": _finalize_agg(dim_agg, top=3),
        "sub_items": _finalize_agg(sub_agg, top=3),
    }


def _finalize_agg(agg: dict, top: int) -> list[dict]:
    items = sorted(agg.values(), key=lambda x: (-x["loss_total"], -x["occurrence_count"]))
    result = []
    for item in items[:top]:
        result.append(
            {
                "key": item["key"],
                "name": item["name"],
                "loss_total": item["loss_total"],
                "occurrence_count": item["occurrence_count"],
                "avg_score": round(item["score_sum"] / item["occurrence_count"], 1)
                if item["occurrence_count"]
                else None,
            }
        )
    return result
