# 设置页 API：我的模型 / 质检模板 / 应用信息
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.db.database import get_db
from backend.schemas.api import ModelConfigIn, TemplateIn
from backend.services import settings_service

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
