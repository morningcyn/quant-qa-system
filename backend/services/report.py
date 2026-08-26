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
