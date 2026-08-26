# 质检触发与历史记录
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.db import repository
from backend.db.database import get_db
from backend.schemas.api import InspectionCreate
from backend.services import pipeline, report
from backend.utils.errors import BizError

router = APIRouter(tags=["inspections"])


@router.post("/assistants/{assistant_id}/inspections", status_code=201)
async def create_inspection(
    assistant_id: int, body: InspectionCreate, session: Session = Depends(get_db)
):
    inspection = await pipeline.run_inspection(
        session, assistant_id, body.raw_dialogue, body.session_title, body.evaluatee
    )
    return report.build_report_view(session, inspection)


@router.get("/inspections")
def list_inspections(
    assistant_id: int | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    is_yellow_alert: bool | None = None,
    session: Session = Depends(get_db),
):
    rows, total = repository.list_inspections(
        session, assistant_id=assistant_id, page=page, page_size=page_size,
        is_yellow_alert=is_yellow_alert,
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": i.id,
                "assistant_id": i.assistant_id,
                "session_title": i.session_title,
                "total_score": i.total_score,
                "is_yellow_alert": i.is_yellow_alert,
                "template_type": i.template_type,
                "turn_count": i.turn_count,
                "customer_profile": i.customer_profile,
                "created_at": i.created_at.isoformat(sep=" ", timespec="seconds"),
            }
            for i in rows
        ],
    }


@router.get("/inspections/{inspection_id}")
def get_inspection(inspection_id: int, session: Session = Depends(get_db)):
    obj = repository.get_inspection(session, inspection_id)
    if obj is None:
        raise BizError("not_found", "质检记录不存在", status_code=404)
    return report.build_report_view(session, obj)


@router.delete("/inspections/{inspection_id}", status_code=204)
def delete_inspection(inspection_id: int, session: Session = Depends(get_db)):
    obj = repository.get_inspection(session, inspection_id)
    if obj is None:
        raise BizError("not_found", "质检记录不存在", status_code=404)
    repository.delete_inspection(session, obj)
    return None
