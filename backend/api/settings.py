# 设置页 API：我的模型 / 质检模板 / 应用信息
import sqlite3

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.db.database import get_db
from backend.schemas.api import DatabaseBackupCleanupIn, DatabaseRestoreIn, ModelConfigIn, TemplateIn
from backend.services import db_backup, settings_service
from backend.utils.errors import BizError

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/models")
def get_models(session: Session = Depends(get_db)):
    return settings_service.list_models(session)


@router.post("/models", status_code=201)
def upsert_model(body: ModelConfigIn, session: Session = Depends(get_db)):
    return settings_service.upsert_model(
        session,
        model_id=body.id,
        name=body.name,
        protocol=body.protocol,
        base_url=body.base_url,
        api_key=body.api_key,
        model_name=body.model_name,
        temperature=body.temperature,
    )


@router.delete("/models/{model_id}", status_code=204)
def delete_model(model_id: str, session: Session = Depends(get_db)):
    settings_service.delete_model(session, model_id)
    return None


@router.put("/models/{model_id}/activate")
def activate_model(model_id: str, session: Session = Depends(get_db)):
    return settings_service.activate_model(session, model_id)


@router.post("/models/{model_id}/test")
async def test_model(model_id: str, session: Session = Depends(get_db)):
    return await settings_service.test_model(session, model_id)


@router.get("/templates")
def get_templates(session: Session = Depends(get_db)):
    return settings_service.list_templates(session)


@router.put("/templates/{template_type}")
def save_template(template_type: str, body: TemplateIn, session: Session = Depends(get_db)):
    return settings_service.save_template(session, template_type, body.name, body.config)


@router.post("/templates/{template_type}/reset")
def reset_template(template_type: str, session: Session = Depends(get_db)):
    return settings_service.reset_template(session, template_type)


@router.get("/app")
def get_app():
    return settings_service.get_app_info()


@router.post("/database/backup")
def create_database_backup():
    path = db_backup.create_backup()
    return {
        "filename": path.name,
        "path": str(path),
        "size": path.stat().st_size,
    }


@router.post("/database/restore")
def restore_database(body: DatabaseRestoreIn):
    if not body.confirm:
        raise BizError("confirmation_required", "恢复数据库前必须明确确认覆盖当前数据", status_code=400)
    try:
        restored_path, safety_path = db_backup.restore_backup(body.filename)
    except ValueError as exc:
        raise BizError("validation_error", str(exc), status_code=400) from exc
    except FileNotFoundError as exc:
        raise BizError("not_found", str(exc), status_code=404) from exc
    except (OSError, sqlite3.DatabaseError) as exc:
        raise BizError("restore_failed", f"数据库恢复失败：{exc}", status_code=409) from exc
    return {
        "restored_from": restored_path.name,
        "safety_backup": safety_path.name,
        "restart_required": True,
    }


@router.get("/database/integrity")
def inspect_database_integrity():
    return db_backup.inspect_database()


@router.get("/database/backups")
def list_database_backups():
    return {"items": db_backup.list_backups()}


@router.get("/database/backups/cleanup-preview")
def preview_database_backup_cleanup():
    return db_backup.plan_backup_cleanup()


@router.post("/database/backups/cleanup")
def cleanup_database_backups(body: DatabaseBackupCleanupIn):
    if not body.confirm:
        raise BizError("confirmation_required", "清理备份前必须明确确认删除旧备份", status_code=400)
    try:
        return db_backup.cleanup_backups(confirm=True)
    except ValueError as exc:
        raise BizError("validation_error", str(exc), status_code=400) from exc
    except OSError as exc:
        raise BizError("cleanup_failed", f"备份清理失败：{exc}", status_code=409) from exc
