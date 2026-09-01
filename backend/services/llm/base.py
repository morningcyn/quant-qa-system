# LLM 客户端抽象与错误分类
from abc import ABC, abstractmethod


class LLMError(Exception):
    """统一 LLM 错误：code 供前端识别与友好提示。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code  # not_configured | auth | rate_limit | network | timeout | bad_json | unknown
        self.message = message


class BaseLLMClient(ABC):
    supports_json_mode: bool = False

    def __init__(self, model: str, timeout: float = 180.0):
        self.model = model
        self.timeout = timeout

    async def aclose(self) -> None:
        """Release client resources when the caller owns the client lifecycle."""
        return None

    @abstractmethod
    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        json_mode: bool = False,
        max_tokens: int = 8192,
    ) -> str:
        """返回模型输出原文。失败抛 LLMError。"""
