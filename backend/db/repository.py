# 数据访问层：service/API 只通过这里读写数据库
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from backend.db.models import Assistant, Inspection, InspectionDetail, ScoreTemplate, ServiceOverview, Setting


# ---------- assistants ----------

def create_assistant(
    session: Session, name: str, employee_no: str, template_type: str, teacher_persona: str = ""
) -> Assistant:
    obj = Assistant(
        name=name,
        employee_no=employee_no,
        template_type=template_type,
        teacher_persona=teacher_persona or "",
    )
    session.add(obj)
    session.commit()
    return obj


def get_assistant(session: Session, assistant_id: int) -> Assistant | None:
    return session.get(Assistant, assistant_id)


def get_assistant_by_no(session: Session, employee_no: str) -> Assistant | None:
    return session.scalar(select(Assistant).where(Assistant.employee_no == employee_no))


def list_assistants(session: Session, q: str | None = None, template_type: str | None = None) -> list[Assistant]:
    stmt = select(Assistant).order_by(Assistant.created_at.desc())
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Assistant.name.like(like), Assistant.employee_no.like(like)))
    if template_type:
        stmt = stmt.where(Assistant.template_type == template_type)
    return list(session.scalars(stmt))


def update_assistant(
    session: Session, obj: Assistant, name: str, template_type: str, teacher_persona: str = ""
) -> Assistant:
    obj.name = name
    obj.template_type = template_type
    obj.teacher_persona = teacher_persona or ""
    session.commit()
    return obj


def delete_assistant(session: Session, obj: Assistant) -> None:
    session.delete(obj)
    session.commit()


# ---------- inspections ----------

def save_inspection(
    session: Session,
    assistant_id: int,
    session_title: str | None,
    total_score: int,
    is_yellow_alert: bool,
    yellow_alert_reasons: list[str],
    is_red_alert: bool = False,
    red_alert_reasons: list[str] | None = None,
    template_type: str = "standard",
    template_snapshot: dict | None = None,
    turn_count: int = 0,
    customer_profile: str | None = None,
    raw_dialogue: str = "",
    d_scores: dict | None = None,
    s_scores: dict | None = None,
    highlight_dialogue: list[dict] | None = None,
    suggestions: list[str] | None = None,
    evaluatee: str | None = None,
    na_dims: list[dict] | None = None,
    effective_max: int | None = None,
) -> Inspection:
    inspection = Inspection(
        assistant_id=assistant_id,
        session_title=session_title,
        total_score=total_score,
        is_yellow_alert=is_yellow_alert,
        yellow_alert_reasons_json=json.dumps(yellow_alert_reasons, ensure_ascii=False),
        is_red_alert=bool(is_red_alert),
        red_alert_reasons_json=json.dumps(red_alert_reasons or [], ensure_ascii=False),
        template_type=template_type,
        template_snapshot_json=json.dumps(template_snapshot, ensure_ascii=False),
        turn_count=turn_count,
        customer_profile=customer_profile,
        evaluatee=evaluatee,
        na_dims_json=json.dumps(na_dims or [], ensure_ascii=False),
        effective_max=effective_max,
    )
    session.add(inspection)
    session.flush()
    session.add(
        InspectionDetail(
            inspection_id=inspection.id,
            raw_dialogue=raw_dialogue or "",
            d_scores_json=json.dumps(d_scores or {}, ensure_ascii=False),
            s_scores_json=json.dumps(s_scores or {}, ensure_ascii=False),
            highlight_dialogue_json=json.dumps(highlight_dialogue or [], ensure_ascii=False),
            suggestions_json=json.dumps(suggestions or [], ensure_ascii=False),
        )
    )
    session.commit()
    return inspection


def get_inspection(session: Session, inspection_id: int) -> Inspection | None:
    return session.get(Inspection, inspection_id)


def set_inspection_conversation(session: Session, inspection_id: int, conversation_id: str) -> None:
    """多人质检：报告归属的客户服务会话 ID（批量完成后补写，单助理路径不调用）。"""
    obj = session.get(Inspection, inspection_id)
    if obj is None:
        return
    obj.conversation_id = conversation_id
    session.commit()


# ---------- service_overviews（多人质检总览） ----------

def save_overview(
    session: Session,
    conversation_id: str,
    title: str | None,
    raw_dialogue: str,
    summary: dict,
    degraded: bool,
    inspection_ids: list[int],
) -> ServiceOverview:
    overview = ServiceOverview(
        conversation_id=conversation_id,
        title=(title or "").strip() or None,
        raw_dialogue=raw_dialogue,
        summary_json=json.dumps(summary, ensure_ascii=False),
        degraded=degraded,
        inspection_ids_json=json.dumps(inspection_ids, ensure_ascii=False),
    )
    session.add(overview)
    session.commit()
    return overview


def get_overview(session: Session, overview_id: int) -> ServiceOverview | None:
    return session.get(ServiceOverview, overview_id)


def list_overviews(
    session: Session,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[ServiceOverview], int]:
    """多人质检总览列表（按生成时间倒序），供「多人质检」页历史总览回看。"""
    stmt = select(ServiceOverview).order_by(ServiceOverview.created_at.desc(), ServiceOverview.id.desc())
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(
        session.scalars(stmt.offset((page - 1) * page_size).limit(page_size))
    )
    return rows, total


def list_inspections(
    session: Session,
    assistant_id: int | None = None,
    page: int = 1,
    page_size: int = 20,
    is_yellow_alert: bool | None = None,
) -> tuple[list[Inspection], int]:
    stmt = select(Inspection).order_by(Inspection.created_at.desc(), Inspection.id.desc())
    if assistant_id is not None:
        stmt = stmt.where(Inspection.assistant_id == assistant_id)
    if is_yellow_alert is not None:
        stmt = stmt.where(Inspection.is_yellow_alert == is_yellow_alert)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(
        session.scalars(stmt.offset((page - 1) * page_size).limit(page_size))
    )
    return rows, total


def delete_inspection(session: Session, obj: Inspection) -> None:
    session.delete(obj)
    session.commit()


def get_inspection_detail(session: Session, inspection_id: int) -> InspectionDetail | None:
    return session.scalar(
        select(InspectionDetail).where(InspectionDetail.inspection_id == inspection_id)
    )


def inspection_score_rows_since(
    session: Session, assistant_id: int, since: datetime
) -> list[tuple[Inspection, InspectionDetail | None]]:
    """取某员工自 since 起的质检主表+详情（用于趋势与 Top3 聚合）。"""
    stmt = (
        select(Inspection, InspectionDetail)
        .outerjoin(InspectionDetail, InspectionDetail.inspection_id == Inspection.id)
        .where(Inspection.assistant_id == assistant_id, Inspection.created_at >= since)
        .order_by(Inspection.created_at)
    )
    return [(i, d) for i, d in session.execute(stmt)]


def assistant_stats_since(session: Session, since: datetime) -> dict[int, dict]:
    """员工列表聚合：近 N 天质检次数 / 均分 / 黄灯次数 + 最近一次得分。"""
    from sqlalchemy import case

    agg = session.execute(
        select(
            Inspection.assistant_id,
            func.count().label("cnt"),
            func.avg(Inspection.total_score).label("avg"),
            func.sum(case((Inspection.is_yellow_alert == True, 1), else_=0)).label("yellow"),  # noqa: E712
        )
        .where(Inspection.created_at >= since)
        .group_by(Inspection.assistant_id)
    ).all()
    subq = (
        select(
            Inspection.assistant_id,
            Inspection.total_score,
            func.row_number()
            .over(
                partition_by=Inspection.assistant_id,
                order_by=(Inspection.created_at.desc(), Inspection.id.desc()),
            )
            .label("rn"),
        ).subquery()
    )
    latest = session.execute(
        select(subq.c.assistant_id, subq.c.total_score).where(subq.c.rn == 1)
    ).all()
    latest_map = {aid: score for aid, score in latest}
    result: dict[int, dict] = {}
    for aid, cnt, avg, yellow in agg:
        result[aid] = {
            "count": int(cnt),
            "avg_score": round(float(avg), 1),
            "yellow_count": int(yellow or 0),
            "latest_score": latest_map.get(aid),
        }
    return result


# ---------- score templates ----------

def list_templates(session: Session) -> list[ScoreTemplate]:
    return list(session.scalars(select(ScoreTemplate).order_by(ScoreTemplate.id)))


def get_template(session: Session, template_type: str) -> ScoreTemplate | None:
    return session.scalar(
        select(ScoreTemplate).where(ScoreTemplate.template_type == template_type)
    )


def upsert_template(
    session: Session, template_type: str, name: str, config: dict
) -> ScoreTemplate:
    obj = get_template(session, template_type)
    if obj is None:
        obj = ScoreTemplate(template_type=template_type, name=name, config_json="{}")
        session.add(obj)
    obj.name = name
    obj.config_json = json.dumps(config, ensure_ascii=False)
    obj.updated_at = datetime.now()
    session.commit()
    return obj


# ---------- settings ----------

def get_setting_value(session: Session, key: str) -> str | None:
    obj = session.scalar(select(Setting).where(Setting.key == key))
    return obj.value if obj else None


def get_setting_json(session: Session, key: str, default: Any = None) -> Any:
    value = get_setting_value(session, key)
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def set_setting_value(session: Session, key: str, value: str) -> None:
    obj = session.scalar(select(Setting).where(Setting.key == key))
    if obj is None:
        session.add(Setting(key=key, value=value))
    else:
        obj.value = value
        obj.updated_at = datetime.now()
    session.commit()


def set_setting_json(session: Session, key: str, value: Any) -> None:
    set_setting_value(session, key, json.dumps(value, ensure_ascii=False))


def delete_setting(session: Session, key: str) -> None:
    session.execute(delete(Setting).where(Setting.key == key))
    session.commit()


# ---------- 通用 ----------

def now_utc_naive() -> datetime:
    return datetime.now()
