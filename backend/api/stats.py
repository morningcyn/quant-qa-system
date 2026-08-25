# 下属主页统计：近30天趋势 / Top3 失分
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.db import repository
from backend.db.database import get_db
from backend.services import report
from backend.utils.errors import BizError

router = APIRouter(prefix="/assistants/{assistant_id}/stats", tags=["stats"])


def _ensure_assistant(session: Session, assistant_id: int) -> None:
    if repository.get_assistant(session, assistant_id) is None:
        raise BizError("not_found", "员工不存在", status_code=404)


@router.get("/trend")
def trend(
    assistant_id: int,
    days: int = Query(30, ge=7, le=365),
    session: Session = Depends(get_db),
):
    _ensure_assistant(session, assistant_id)
    return report.trend_stats(session, assistant_id, days=days)


@router.get("/top3")
def top3(
    assistant_id: int,
    days: int = Query(30, ge=7, le=365),
    session: Session = Depends(get_db),
):
    _ensure_assistant(session, assistant_id)
    return report.top3_loss(session, assistant_id, days=days)
