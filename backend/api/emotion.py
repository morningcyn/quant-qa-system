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

    curve 字段：新行（summary_json 已含 curve）直接透传；
    旧行（情绪曲线上线前生成）→ 由落库快照实时派生（纯计算，零 LLM），
    并给 timeline 补 emotion_score，保证前端曲线数据自包含。
    """
    summary = json.loads(obj.summary_json)
    curve = summary.get("curve")
    if curve is None:
        try:
            items = json.loads(obj.items_json) or []
        except (TypeError, ValueError):
            items = []
        try:
            rows = json.loads(obj.messages_json) or []
        except (TypeError, ValueError):
            rows = []
        curve = build_curve(rows, items)
        # 旧行 timeline 无 emotion_score → 补算（不影响落库，仅本次响应）
        for it in summary.get("timeline") or []:
            if it.get("emotion_score") is None:
                it["emotion_score"] = EMOTION_SCORE.get(it.get("emotion"), 0)
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
