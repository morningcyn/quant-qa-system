# 模型配置 CRUD / 连通性测试 / 质检模板配置
import time
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from backend.config import APP_VERSION, DATA_DIR, DB_PATH, DEFAULT_TEMPLATES
from backend.db import repository
from backend.services import scoring
from backend.services.llm import factory
from backend.services.llm.base import LLMError
from backend.utils import crypto
from backend.utils.errors import BizError

PRESET_BASE_URLS = {
    "openai_compat": "https://api.deepseek.com/v1",
    "anthropic": "https://api.anthropic.com",
}

MODEL_SUGGESTIONS = {
    "openai_compat": ["deepseek-chat", "deepseek-reasoner", "glm-4-flash", "qwen-plus", "gpt-4o-mini", "gpt-4.1"],
    "anthropic": ["claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929", "claude-opus-4-1"],
}


def _masked(cfg: dict) -> dict:
    out = {k: v for k, v in cfg.items() if k != "api_key_encrypted"}
    out["has_api_key"] = bool(cfg.get("api_key_encrypted"))
    out["api_key_masked"] = "已保存（仅本机可解密）" if cfg.get("api_key_encrypted") else ""
    return out


def list_models(session: Session) -> dict:
    configs = factory.get_model_configs(session)
    active_id = repository.get_setting_json(session, factory.KEY_ACTIVE_MODEL_ID, None)
    return {
        "models": [_masked(cfg) | {"is_active": str(cfg.get("id")) == str(active_id)} for cfg in configs],
        "active_model_id": active_id,
        "preset_base_urls": PRESET_BASE_URLS,
        "model_suggestions": MODEL_SUGGESTIONS,
    }


def upsert_model(
    session: Session,
    *,
    model_id: str | None,
    name: str,
    protocol: str,
    base_url: str,
    api_key: str,
    model_name: str,
    temperature: float,
) -> dict:
    if protocol not in ("openai_compat", "anthropic"):
        raise BizError("validation_error", "不支持的协议类型")
    configs = factory.get_model_configs(session)
    if model_id:
        target = next((c for c in configs if str(c.get("id")) == str(model_id)), None)
        if target is None:
            raise BizError("not_found", "模型配置不存在", status_code=404)
        target.update(
            name=name.strip() or "未命名模型",
            protocol=protocol,
            base_url=base_url.strip(),
            model_name=model_name.strip(),
            temperature=temperature,
        )
        if api_key.strip():  # 空 = 保留旧密钥
            target["api_key_encrypted"] = crypto.encrypt_secret(api_key.strip())
            target.pop("api_key", None)
        saved = target
    else:
        if not api_key.strip():
            raise BizError("validation_error", "请填写 API Key")
        if not model_name.strip():
            raise BizError("validation_error", "请填写模型名称")
        saved = {
            "id": uuid.uuid4().hex[:8],
            "name": name.strip() or "未命名模型",
            "protocol": protocol,
            "base_url": base_url.strip() or PRESET_BASE_URLS[protocol],
            "api_key_encrypted": crypto.encrypt_secret(api_key.strip()),
            "model_name": model_name.strip(),
            "temperature": temperature,
            "last_test_status": None,
            "last_test_at": None,
        }
        configs.append(saved)
    repository.set_setting_json(session, factory.KEY_LLM_MODELS, configs)
    if repository.get_setting_json(session, factory.KEY_ACTIVE_MODEL_ID, None) is None:
        repository.set_setting_json(session, factory.KEY_ACTIVE_MODEL_ID, saved["id"])
    return _masked(saved)


def delete_model(session: Session, model_id: str) -> None:
    configs = factory.get_model_configs(session)
    configs = [c for c in configs if str(c.get("id")) != str(model_id)]
    repository.set_setting_json(session, factory.KEY_LLM_MODELS, configs)
    active_id = repository.get_setting_json(session, factory.KEY_ACTIVE_MODEL_ID, None)
    if str(active_id) == str(model_id):
        repository.set_setting_json(
            session, factory.KEY_ACTIVE_MODEL_ID, configs[0]["id"] if configs else None
        )


def activate_model(session: Session, model_id: str) -> dict:
    configs = factory.get_model_configs(session)
    if not any(str(c.get("id")) == str(model_id) for c in configs):
        raise BizError("not_found", "模型配置不存在", status_code=404)
    repository.set_setting_json(session, factory.KEY_ACTIVE_MODEL_ID, model_id)
    return list_models(session)


async def test_model(session: Session, model_id: str) -> dict:
    """连通性测试：发一条最小消息，20s 超时。"""
    configs = factory.get_model_configs(session)
    cfg = next((c for c in configs if str(c.get("id")) == str(model_id)), None)
    if cfg is None:
        raise BizError("not_found", "模型配置不存在", status_code=404)
    plain = crypto.decrypt_secret(cfg.get("api_key_encrypted") or "")
    if not plain:
        result = {"ok": False, "latency_ms": None, "model": cfg.get("model_name"), "message": "尚未填写或无法解密 API Key，请重新填写"}
        _save_test_result(session, cfg, result)
        return result
    client_cfg = dict(cfg) | {"api_key": plain}
    from backend.services.llm.factory import build_client

    client = build_client(client_cfg, timeout=20.0)
    started = time.perf_counter()
    try:
        await client.complete("", "请只回复：OK", temperature=0.0, max_tokens=16)
        latency_ms = int((time.perf_counter() - started) * 1000)
        result = {"ok": True, "latency_ms": latency_ms, "model": cfg.get("model_name"), "message": "连接成功"}
    except LLMError as exc:
        result = {"ok": False, "latency_ms": None, "model": cfg.get("model_name"), "message": exc.message}
    finally:
        await client.aclose()
    _save_test_result(session, cfg, result)
    return result


def _save_test_result(session: Session, cfg: dict, result: dict) -> None:
    configs = factory.get_model_configs(session)
    for c in configs:
        if str(c.get("id")) == str(cfg.get("id")):
            c["last_test_status"] = "ok" if result["ok"] else "failed"
            c["last_test_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
            break
    repository.set_setting_json(session, factory.KEY_LLM_MODELS, configs)


# ---------- 质检模板 ----------

def list_templates(session: Session) -> dict:
    rows = repository.list_templates(session)
    return {
        "templates": [
            {"template_type": r.template_type, "name": r.name, "config": _safe_json(r.config_json)}
            for r in rows
        ]
    }


def _safe_json(text: str):
    import json

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def save_template(session: Session, template_type: str, name: str, config: dict) -> dict:
    if template_type not in DEFAULT_TEMPLATES:
        raise BizError("validation_error", f"未知模板类型：{template_type}")
    errors = scoring.validate_template_config(config)
    if errors:
        raise BizError("validation_error", "模板配置不合法：" + "；".join(errors))
    row = repository.upsert_template(session, template_type, name or DEFAULT_TEMPLATES[template_type]["name"], config)
    return {"template_type": row.template_type, "name": row.name, "config": _safe_json(row.config_json)}


def reset_template(session: Session, template_type: str) -> dict:
    default = DEFAULT_TEMPLATES.get(template_type)
    if default is None:
        raise BizError("validation_error", f"未知模板类型：{template_type}")
    return save_template(session, template_type, default["name"], default)


def get_app_info() -> dict:
    return {
        "version": APP_VERSION,
        "data_dir": str(DATA_DIR),
        "db_path": str(DB_PATH),
    }
