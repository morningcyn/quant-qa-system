# 员工 CRUD
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.db import repository
from backend.db.database import get_db
from backend.schemas.api import AssistantCreate, AssistantUpdate
from backend.utils.errors import BizError

router = APIRouter(prefix="/assistants", tags=["assistants"])


def _db() -> Session:
    return Depends(get_db)


@router.get("")
def list_assistants(
    q: str | None = None,
    template_type: str | None = None,
    session: Session = _db(),
):
    rows = repository.list_assistants(session, q=q, template_type=template_type)
    stats = repository.assistant_stats_since(
        session, datetime.now() - timedelta(days=29)
    )
    return {
        "assistants": [
            {
                "id": a.id,
                "name": a.name,
                "employee_no": a.employee_no,
                "template_type": a.template_type,
                "teacher_persona": a.teacher_persona or "",
                "created_at": a.created_at.isoformat(sep=" ", timespec="seconds"),
                "stats": stats.get(a.id, {"count": 0, "avg_score": None, "yellow_count": 0, "latest_score": None}),
            }
            for a in rows
        ]
    }


@router.post("", status_code=201)
def create_assistant(body: AssistantCreate, session: Session = _db()):
    if repository.get_assistant_by_no(session, body.employee_no):
        raise BizError("employee_no_conflict", f"工号 {body.employee_no} 已存在", status_code=409)
    obj = repository.create_assistant(
        session, body.name.strip(), body.employee_no.strip(), body.template_type, body.teacher_persona
    )
    return {
        "id": obj.id,
        "name": obj.name,
        "employee_no": obj.employee_no,
        "template_type": obj.template_type,
        "teacher_persona": obj.teacher_persona or "",
    }


@router.get("/{assistant_id}")
def get_assistant(assistant_id: int, session: Session = _db()):
    obj = repository.get_assistant(session, assistant_id)
    if obj is None:
        raise BizError("not_found", "员工不存在", status_code=404)
    return {
        "id": obj.id,
        "name": obj.name,
        "employee_no": obj.employee_no,
        "template_type": obj.template_type,
        "teacher_persona": obj.teacher_persona or "",
        "created_at": obj.created_at.isoformat(sep=" ", timespec="seconds"),
    }


@router.put("/{assistant_id}")
def update_assistant(assistant_id: int, body: AssistantUpdate, session: Session = _db()):
    obj = repository.get_assistant(session, assistant_id)
    if obj is None:
        raise BizError("not_found", "员工不存在", status_code=404)
    obj = repository.update_assistant(
        session, obj, body.name.strip(), body.template_type, body.teacher_persona
    )
    return {
        "id": obj.id,
        "name": obj.name,
        "employee_no": obj.employee_no,
        "template_type": obj.template_type,
        "teacher_persona": obj.teacher_persona or "",
    }


@router.delete("/{assistant_id}", status_code=204)
def delete_assistant(assistant_id: int, session: Session = _db()):
    obj = repository.get_assistant(session, assistant_id)
    if obj is None:
        raise BizError("not_found", "员工不存在", status_code=404)
    repository.delete_assistant(session, obj)
    return None
