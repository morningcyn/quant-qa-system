# 报告视图模型查询
import json

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.db import batch_repository as brepo
from backend.db import repository
from backend.db.database import get_db
from backend.services import report
from backend.utils.errors import BizError

router = APIRouter(prefix="/reports", tags=["reports"])


def _batch_task_report_ids(session: Session, batch_id: str, inspection_id: int) -> list[int]:
    """批量评分批次：conversation_id 即 batch_id，同一批次多客户任务共享该锚点。

    报告页切换栏必须按任务分组（只列同任务各助理的报告，不混入其他客户任务）；
    通过任务 result_json 里的 reports 集合定位所属任务。找不到（老数据）返回空 → 调用方不过滤。
    """
    for t in brepo.list_tasks(session, batch_id):
        result = json.loads(t.result_json) if t.result_json else None
        ids = [r["inspection_id"] for r in (result or {}).get("reports") or []]
        if inspection_id in ids:
            return ids
    return []


@router.get("/{inspection_id}")
def get_report(inspection_id: int, session: Session = Depends(get_db)):
    obj = repository.get_inspection(session, inspection_id)
    if obj is None:
        raise BizError("not_found", "质检记录不存在", status_code=404)
    view = report.build_report_view(session, obj)
    # 同会话多位助理（批量评分多助理任务 / 多人质检）：报告页顶部提供助理切换入口。
    # 仅当同 conversation_id 下确实有多份报告时附加，单助理会话响应不变（向后兼容）。
    if obj.conversation_id:
        siblings = repository.list_inspections_by_conversation(session, obj.conversation_id)
        if len(siblings) > 1:
            # 批量评分批次多任务共享 conversation_id → 按任务隔离；多人质检（uuid 锚点）全量
            if brepo.get_batch(session, obj.conversation_id) is not None:
                ids = _batch_task_report_ids(session, obj.conversation_id, obj.id)
                if ids:
                    siblings = [s for s in siblings if s.id in ids]
            if len(siblings) > 1:
                view["session_reports"] = [
                    {
                        "id": i.id,
                        "assistant_name": i.assistant.name,
                        "total_score": i.total_score,
                        "is_red_alert": bool(i.is_red_alert),
                        "is_yellow_alert": bool(i.is_yellow_alert),
                    }
                    for i in siblings
                ]
    return view
