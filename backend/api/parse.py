# 解析预览：仅解析不评分，供上传对话框防呆前移
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.db import repository
from backend.db.database import get_db
from backend.schemas.api import ParsePreviewIn
from backend.services import multiparser, parser as parser_service

router = APIRouter(prefix="/parse", tags=["parse"])


@router.post("/preview")
def preview(body: ParsePreviewIn, session: Session = Depends(get_db)):
    result = parser_service.parse_raw(body.raw_text)
    return {
        "fmt": result.fmt,
        "turns": [
            {"turn_no": t.turn_no, "role": t.role, "speaker": t.speaker, "text": t.text} for t in result.turns
        ],
        "role_stats": result.role_stats,
        "speakers": result.speakers,  # 助侧去重显示名（助理A/助理B…），供"本次评估对象"必选
        "warnings": result.warnings,
    }


@router.post("/preview-multi")
def preview_multi(body: ParsePreviewIn, session: Session = Depends(get_db)):
    """多人质检预览：完整会话 → 结构化消息 + 助理识别/归并 + 员工自动匹配（纯规则，不调 LLM）。"""
    result = multiparser.parse_multi(body.raw_text, repository.list_assistants(session))
    return {
        "fmt": result.fmt,
        "role_stats": result.role_stats,
        "messages": [
            {
                "turn_no": m.turn_no,
                "role": m.role,
                "speaker": m.speaker,
                "canonical_name": m.canonical_name,
                "text": m.text,
                "timestamp": m.timestamp,
                "assistant_id": m.assistant_id,
                "raw_line": m.raw_line,
            }
            for m in result.messages
        ],
        "assistants": [
            {
                "canonical_name": c.canonical_name,
                "display_name": c.display_name,
                "matched_assistant_id": c.assistant_id,
                "aliases": c.aliases,
                "reply_count": len(c.reply_turn_nos),
                "reply_turn_nos": c.reply_turn_nos,
                "turn_range": f"{c.reply_turn_nos[0]}-{c.reply_turn_nos[-1]}" if c.reply_turn_nos else "",
            }
            for c in result.clusters
        ],
        "warnings": result.warnings,
    }
