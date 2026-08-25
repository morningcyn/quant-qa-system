# 模型配置解析与客户端工厂：读 settings → 解密 → 按协议构建客户端
from sqlalchemy.orm import Session

from backend.db import repository
from backend.services.llm.anthropic_client import AnthropicClient
from backend.services.llm.base import BaseLLMClient, LLMError
from backend.services.llm.openai_compat import OpenAICompatClient
from backend.utils import crypto

KEY_LLM_MODELS = "llm_models"
KEY_ACTIVE_MODEL_ID = "active_model_id"


def get_model_configs(session: Session) -> list[dict]:
    return repository.get_setting_json(session, KEY_LLM_MODELS, []) or []


def get_active_model(session: Session, configs: list[dict] | None = None) -> dict | None:
    configs = configs if configs is not None else get_model_configs(session)
    if not configs:
        return None
    active_id = repository.get_setting_json(session, KEY_ACTIVE_MODEL_ID, None)
    for cfg in configs:
        if str(cfg.get("id")) == str(active_id):
            return cfg
    return configs[0]


def build_client(cfg: dict, timeout: float = 180.0) -> BaseLLMClient:
    """cfg 需含 protocol/base_url/model_name/api_key（已解密）。"""
    protocol = cfg.get("protocol", "openai_compat")
    api_key = cfg.get("api_key") or ""
    model = cfg.get("model_name") or ""
    base_url = cfg.get("base_url") or ""
    if not api_key:
        raise LLMError("not_configured", "尚未配置模型 API Key，请前往「我的模型」填写")
    if protocol == "anthropic":
        return AnthropicClient(base_url, api_key, model, timeout=timeout)
    return OpenAICompatClient(base_url, api_key, model, timeout=timeout)


def get_active_runtime(session: Session, timeout: float = 180.0) -> tuple[BaseLLMClient, dict]:
    """解析激活模型配置并构建 (客户端, 已解密配置)。未配置/密钥失效抛 LLMError。"""
    configs = get_model_configs(session)
    if not configs:
        raise LLMError(
            "not_configured",
            "尚未配置模型 API Key，请先到「设置 → 我的模型」添加模型并填写您的 Key（密钥仅保存在本机）",
        )
    cfg = dict(get_active_model(session, configs))
    stored_key = cfg.get("api_key_encrypted") or ""
    plain = crypto.decrypt_secret(stored_key)
    if plain is None:
        raise LLMError(
            "not_configured",
            "已保存的 API Key 无法解密（可能更换了电脑或系统账户），请在「我的模型」中重新填写",
        )
    cfg["api_key"] = plain
    return build_client(cfg, timeout=timeout), cfg


def get_active_client(session: Session, timeout: float = 180.0) -> BaseLLMClient:
    """解析当前激活的模型配置并构建客户端；未配置/密钥失效抛 LLMError。"""
    return get_active_runtime(session, timeout)[0]
