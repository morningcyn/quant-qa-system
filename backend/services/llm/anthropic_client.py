# Anthropic 协议客户端：Claude（无 json_object，靠提示词 + json_guard 兜底）
import httpx

from backend.services.llm.base import BaseLLMClient, LLMError

_ANTHROPIC_VERSION = "2023-06-01"

# Anthropic 错误类型 → 统一 code
_ERROR_TYPE_MAP = {
    "authentication_error": "auth",
    "permission_error": "auth",
    "rate_limit_error": "rate_limit",
    "overloaded_error": "rate_limit",
    "api_error": "network",
}


class AnthropicClient(BaseLLMClient):
    supports_json_mode = False

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 180.0):
        super().__init__(model, timeout)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client = httpx.AsyncClient(trust_env=False)

    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        json_mode: bool = False,  # Anthropic 无 json_object，忽略
        max_tokens: int = 8192,
    ) -> str:
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        try:
            resp = await self._client.post(
                f"{self.base_url}/v1/messages",
                json=body,
                headers=headers,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
            )
        except httpx.TimeoutException as exc:
            raise LLMError("timeout", "模型请求超时，请稍后重试或检查网络") from exc
        except httpx.ConnectError as exc:
            raise LLMError("network", f"无法连接模型服务（{self.base_url}），请检查网络与地址") from exc
        except httpx.HTTPError as exc:
            raise LLMError("network", "模型请求网络异常") from exc
        if resp.status_code != 200:
            code, detail = _parse_error(resp)
            message = _friendly_message(code, resp.status_code, detail)
            raise LLMError(code, message)
        try:
            data = resp.json()
            parts = data.get("content") or []
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            if not text:
                raise LLMError("unknown", "模型服务返回内容为空")
            return text
        except (ValueError, TypeError) as exc:
            raise LLMError("unknown", "模型服务返回格式异常") from exc

    async def aclose(self) -> None:
        await self._client.aclose()


def _parse_error(resp: httpx.Response) -> tuple[str, str]:
    detail = ""
    try:
        data = resp.json()
        err = data.get("error") or {}
        code = _ERROR_TYPE_MAP.get(str(err.get("type", "")), "unknown")
        detail = str(err.get("message") or "")[:200]
    except ValueError:
        code = "unknown"
        detail = resp.text[:200]
    return code, detail


def _friendly_message(code: str, status: int, detail: str) -> str:
    if code == "auth":
        return "API Key 无效或无权限，请在「我的模型」中检查密钥"
    if code == "rate_limit":
        return "请求过于频繁或额度不足，请稍后重试或检查账户余额"
    if code == "network":
        return "模型服务暂时不可用，请稍后重试"
    return f"模型服务返回错误（{status}）：{detail}"
