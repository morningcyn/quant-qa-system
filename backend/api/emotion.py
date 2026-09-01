# 客户情绪分析 API：报告页「生成情绪分析」+ 情绪数据查询（批量任务评分时已自动生成）
import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.db import repository
from backend.db.database import get_db
from backend.services.emotion.analyzer import analyze_session, resolve_inspection_context
from backend.services.emotion.derive import EMOTION_SCORE, build_curve
from backend.services.llm import factory
from backend.utils.errors import BizError

router = APIRouter(prefix="/emotion", tags=["emotion"])


class AnalyzeIn(BaseModel):
    inspection_id: int


def _view(session: Session, obj) -> dict:
    """EmotionSession 行 → 视图模型（summary + meta 组装）。

    curve 字段由落库快照实时派生（纯计算，零 LLM），兼容新旧情绪行，
    保证历史数据也使用统一的时间排序和助理回复绑定规则。
    """
    summary = json.loads(obj.summary_json)
    curve = summary.get("curve")
    # 曲线是由快照确定性派生的：每次读取都重新计算，确保旧版本已落库的
    # turn_no 曲线也能按真实时间戳和新的助理绑定规则展示，不改动历史落库数据。
    try:
        items = json.loads(obj.items_json) or []
    except (TypeError, ValueError):
        items = []
    try:
        rows = json.loads(obj.messages_json) or []
    except (TypeError, ValueError):
        rows = []
    if rows or items:
        curve = build_curve(rows, items)
    elif curve is None:
        curve = build_curve([], [])
    # 旧行 timeline 无 emotion_score → 补算（不影响落库，仅本次响应）
    for it in summary.get("timeline") or []:
        if it.get("emotion_score") is None:
            it["emotion_score"] = EMOTION_SCORE.get(it.get("emotion"), 0)
    # 情绪时间线列表与曲线共用同一客户点顺序；仅调整本次响应，不改写历史摘要。
    point_order = {point["turn_no"]: index for index, point in enumerate(curve.get("points") or [])}
    if point_order and summary.get("timeline"):
        summary["timeline"] = sorted(
            summary["timeline"],
            key=lambda item: point_order.get(item.get("turn_no"), len(point_order)),
        )
        summary["current"] = summary["timeline"][-1]
    return {
        "conversation_id": obj.conversation_id,
        "source_type": obj.source_type,
        "customer_name": obj.customer_name,
        "title": obj.title,
        "created_at": obj.created_at.isoformat(sep=" ", timespec="seconds") if obj.created_at else None,
        "degraded": obj.degraded,
        "warning": obj.warning,
        **summary,
        "curve": curve,
    }


def _get_or_404(session: Session, inspection_id: int):
    inspection = repository.get_inspection(session, inspection_id)
    if inspection is None:
        raise BizError("not_found", "质检记录不存在", status_code=404)
    return inspection


@router.post("/analyze")
async def analyze(body: AnalyzeIn, session: Session = Depends(get_db)):
    """对指定报告的客户会话做情绪分析（幂等：重复调用 = 重新分析覆盖）。
    批量评分任务的情绪分析由后台自动完成，这里主要覆盖多人质检/老批次报告。"""
    inspection = _get_or_404(session, body.inspection_id)
    ctx = resolve_inspection_context(session, inspection, need_messages=True)
    if not any(getattr(m, "role", None) == "客" for m in ctx["msgs"]):
        raise BizError("no_customer_message", "该会话无客户消息，无法进行情绪分析", status_code=400)
    client, cfg = factory.get_active_runtime(session)
    emo = await analyze_session(
        session,
        msgs=ctx["msgs"],
        title=ctx["title"],
        conversation_id=ctx["conversation_id"],
        source_type=ctx["source_type"],
        customer_name=ctx["customer_name"],
        client=client,
        cfg=cfg,
        warning=ctx.get("warning"),
    )
    if emo is None:
        raise BizError("no_customer_message", "该会话无客户消息，无法进行情绪分析", status_code=400)
    return _view(session, emo)


@router.get("/inspection/{inspection_id}")
def get_emotion(inspection_id: int, session: Session = Depends(get_db)):
    """查询指定报告的客户情绪分析（报告页卡片取数；未生成 → emotion_not_found）。"""
    inspection = _get_or_404(session, inspection_id)
    ctx = resolve_inspection_context(session, inspection, need_messages=False)
    obj = repository.get_emotion_session_by_conversation(session, ctx["conversation_id"])
    if obj is None:
        raise BizError("emotion_not_found", "该会话尚未生成情绪分析", status_code=404)
    return _view(session, obj)
