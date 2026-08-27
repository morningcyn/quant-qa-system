# 多人质检总览：本次客户服务总览（参与者 + LLM/规则汇总 + 完整原始聊天记录）
import json

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.db import repository
from backend.db.database import get_db
from backend.services import report
from backend.utils.errors import BizError

router = APIRouter(prefix="/overviews", tags=["overviews"])


def _loads(text: str, default):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


@router.get("")
def list_overviews(
    page: int = 1,
    page_size: int = 20,
    session: Session = Depends(get_db),
):
    """历史总览列表（按生成时间倒序），供「多人质检」页回看任意一次质检对比。"""
    page_size = max(1, min(page_size, 50))
    rows, total = repository.list_overviews(session, page=page, page_size=page_size)
    items = []
    for ov in rows:
        data = _loads(ov.summary_json, {})
        participants = data.get("participants", [])
        items.append(
            {
                "id": ov.id,
                "conversation_id": ov.conversation_id,
                "title": (ov.title or "").strip() or "未命名会话",
                "created_at": ov.created_at.isoformat(sep=" ", timespec="seconds"),
                "participant_count": len(participants),
                "degraded": ov.degraded,
            }
        )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/{overview_id}")
def get_overview(overview_id: int, session: Session = Depends(get_db)):
    ov = repository.get_overview(session, overview_id)
    if ov is None:
        raise BizError("not_found", "总览不存在", status_code=404)
    data = _loads(ov.summary_json, {})
    summary = data.get("summary", {})
    participants = data.get("participants", [])
    # 报告视图实时重建（关联报告可能被删除，缺失项跳过）
    inspection_ids = _loads(ov.inspection_ids_json, [])
    reports = {}
    for iid in inspection_ids:
        ins = repository.get_inspection(session, iid)
        if ins is not None:
            reports[iid] = report.build_report_view(session, ins)
    for p in participants:
        r = reports.get(p.get("inspection_id"))
        p["report"] = r
        # 「可借鉴」优点：实时从报告维度确定性推导（不落库，新旧总览数据一致）
        p["strengths"] = report.derive_strengths(r) if r else []
    return {
        "id": ov.id,
        "conversation_id": ov.conversation_id,
        "title": ov.title,
        "created_at": ov.created_at.isoformat(sep=" ", timespec="seconds"),
        "degraded": ov.degraded,
        "summary": summary,
        "participants": participants,
        "raw_dialogue": ov.raw_dialogue,
    }
