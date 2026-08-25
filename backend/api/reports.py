# 报告视图模型查询
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.db import repository
from backend.db.database import get_db
from backend.services import report
from backend.utils.errors import BizError

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/{inspection_id}")
def get_report(inspection_id: int, session: Session = Depends(get_db)):
    obj = repository.get_inspection(session, inspection_id)
    if obj is None:
        raise BizError("not_found", "质检记录不存在", status_code=404)
    return report.build_report_view(session, obj)
