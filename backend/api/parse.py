# 解析预览：仅解析不评分，供上传对话框防呆前移
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.db.database import get_db
from backend.schemas.api import ParsePreviewIn
from backend.services import parser as parser_service

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
