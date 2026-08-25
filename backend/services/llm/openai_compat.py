# OpenAI 兼容协议客户端：DeepSeek / GLM / Qwen / GPT 等
import httpx

from backend.services.llm.base import BaseLLMClient, LLMError

_STATUS_CODE_MAP = {
    401: "auth",
    403: "auth",
    429: "rate_limit",
    408: "timeout",
}


class OpenAICompatClient(BaseLLMClient):
    supports_json_mode = True

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 180.0):
        super().__init__(model, timeout)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client = httpx.AsyncClient(trust_env=False)  # 防本机系统代理劫持

    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        json_mode: bool = False,
        max_tokens: int = 8192,
    ) -> str:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            resp = await self._client.post(
                f"{self.base_url}/chat/completions",
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
            code = _STATUS_CODE_MAP.get(resp.status_code, "unknown")
            detail = _safe_error_detail(resp)
            message = _friendly_message(code, resp.status_code, detail)
            raise LLMError(code, message)
        try:
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("unknown", "模型服务返回格式异常") from exc

    async def aclose(self) -> None:
        await self._client.aclose()


def _safe_error_detail(resp: httpx.Response) -> str:
    """提取错误详情（脱敏：只取 error 字段，绝不打印 header）。"""
    try:
        data = resp.json()
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:200]
        return str(data)[:200]
    except ValueError:
        return resp.text[:200]


def _friendly_message(code: str, status: int, detail: str) -> str:
    if code == "auth":
        return "API Key 无效或无权限（401），请在「我的模型」中检查密钥"
    if code == "rate_limit":
        return "请求过于频繁或额度不足（429），请稍后重试或检查账户余额"
    if code == "timeout":
        return "模型服务响应超时（408），请稍后重试"
    return f"模型服务返回错误（{status}）：{detail}"
