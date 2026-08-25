# JSON 输出稳定性保障：提取 / 修复 / 字段级校验 / 重试循环
import json
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from backend.services.llm.base import BaseLLMClient, LLMError

_FENCE_RE = re.compile(r"```(?:json)?\s*", re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def extract_json(text: str) -> str:
    """剥掉 markdown fence 与前后杂话，取首个 { 到末个 }（配平括号定位）。"""
    if not text:
        return ""
    text = _FENCE_RE.sub("", text).strip()
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    in_str = False
    escape = False
    last_brace = -1
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                last_brace = i
                break
    if last_brace == -1:
        return text[start:]
    return text[start : last_brace + 1]


def repair_json(text: str) -> str:
    """整体 parse 失败时兜底：去尾逗号、截到最后一个完整 }（应对 max_tokens 截断）。"""
    repaired = _TRAILING_COMMA_RE.sub(r"\1", text)
    if _json_loads_ok(repaired):
        return repaired
    # 尝试逐步截断到最后一个 } 的位置
    for i in range(len(repaired.rstrip()), 0, -1):
        if repaired[i - 1] == "}":
            candidate = repaired[:i]
            candidate = _TRAILING_COMMA_RE.sub(r"\1", candidate)
            if _json_loads_ok(candidate):
                return candidate
    return repaired


def _json_loads_ok(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except json.JSONDecodeError:
        return False


def parse_and_validate(text: str, schema: type[BaseModel]) -> tuple[Any | None, str | None]:
    """返回 (校验通过的对象, 字段级错误摘要)；对象为 None 时 error 非空。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"JSON 语法错误：{exc}"
    if not isinstance(data, dict):
        return None, "JSON 顶层必须是对象"
    try:
        return schema.model_validate(data), None
    except ValidationError as exc:
        errors = []
        for err in exc.errors()[:8]:
            loc = ".".join(str(x) for x in err["loc"])
            errors.append(f"字段 {loc or '(根)'} 错误：{err['msg']}")
        return None, "；".join(errors)


async def complete_json(
    client: BaseLLMClient,
    system: str,
    user: str,
    schema: type[BaseModel],
    *,
    retries: int = 3,
    temperature: float = 0.2,
    max_tokens: int = 8192,
) -> Any:
    """循环：调用 → 提取 → 修复 → 字段级校验；失败把错误摘要反馈给模型重试。"""
    current_user = user
    last_err = "未知错误"
    for attempt in range(retries + 1):
        raw = await client.complete(
            system,
            current_user,
            temperature=temperature,
            json_mode=client.supports_json_mode,
            max_tokens=max_tokens,
        )
        candidate = extract_json(raw)
        result, err = parse_and_validate(candidate, schema)
        if result is not None:
            return result
        if err:
            last_err = err
        repaired = repair_json(candidate)
        if repaired != candidate:
            result2, err2 = parse_and_validate(repaired, schema)
            if result2 is not None:
                return result2
            if err2:
                last_err = err2
        if attempt < retries:
            current_user = (
                f"{user}\n\n【上次输出 JSON 校验失败】{last_err}\n"
                "请修正上述问题后，重新输出一个完整、合法的 JSON 对象（只输出 JSON，不要多余文字）。"
            )
    raise LLMError("bad_json", f"大模型输出 JSON 校验失败（已重试 {retries} 次）：{last_err}")
