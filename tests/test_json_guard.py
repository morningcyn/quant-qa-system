import asyncio

import pytest
from pydantic import BaseModel

from backend.services.llm.base import LLMError
from backend.services.llm.json_guard import (
    complete_json,
    extract_json,
    parse_and_validate,
    repair_json,
)
from tests.conftest import MockLLMClient

_GOOD_JSON = '{"total_score": 69, "is_yellow_alert": false, "yellow_alert_reasons": []}'


class _Mini(BaseModel):
    total_score: int
    is_yellow_alert: bool


class TestExtract:
    def test_fence_and_prose(self):
        text = "好的，以下是评分结果：\n```json\n" + _GOOD_JSON + "\n```\n希望对您有帮助。"
        assert extract_json(text).startswith("{")

    def test_embedded_object(self):
        text = '前缀 {"a": {"b": 1}} 后缀'
        assert extract_json(text) == '{"a": {"b": 1}}'


class TestRepair:
    def test_trailing_comma(self):
        text = '{"a": 1, "b": [1, 2,],}'
        repaired = repair_json(text)
        assert '"b": [1, 2]' in repaired

    def test_truncated_json(self):
        text = '{"a": 1, "b": {"c": 2, "d": [1, 2, 3]}'  # 被 max_tokens 截断
        repaired = repair_json(text)
        assert repaired.endswith("}")

    def test_repair_ok_parse(self):
        result, err = parse_and_validate('{"total_score": 69, "is_yellow_alert": false}', _Mini)
        assert err is None
        assert result.total_score == 69


class TestParseAndValidate:
    def test_field_error_summary(self):
        result, err = parse_and_validate('{"total_score": "abc", "is_yellow_alert": false}', _Mini)
        assert result is None
        assert "total_score" in err


class TestCompleteJson:
    def test_retry_then_success(self):
        client = MockLLMClient(
            ['{"total_score": "bad"', _GOOD_JSON]  # 第一次坏 JSON，第二次成功
        )
        result = asyncio.run(complete_json(client, "s", "u", _Mini, retries=3))
        assert result.total_score == 69
        assert len(client.calls) == 2
        assert "校验失败" in client.calls[1]["user"]  # 错误摘要已反馈给模型

    def test_all_bad_raises(self):
        client = MockLLMClient(["not json at all", "still not json", "nope", "x"])
        with pytest.raises(LLMError) as excinfo:
            asyncio.run(complete_json(client, "s", "u", _Mini, retries=2))
        assert excinfo.value.code == "bad_json"

    def test_llm_error_propagates(self):
        client = MockLLMClient([LLMError("auth", "key 无效")])
        with pytest.raises(LLMError) as excinfo:
            asyncio.run(complete_json(client, "s", "u", _Mini, retries=3))
        assert excinfo.value.code == "auth"
