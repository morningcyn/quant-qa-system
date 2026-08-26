import asyncio

import pytest

from backend.db import repository
from backend.services import pipeline
from backend.utils.errors import BizError
from tests.conftest import (
    SAMPLE_DIALOGUE,
    MockLLMClient,
    low_llm_json,
    valid_llm_json,
)


@pytest.fixture()
def assistant(session):
    return repository.create_assistant(session, "张三", "E001", "standard")


class TestPipeline:
    def test_full_chain_success(self, session, assistant):
        client = MockLLMClient([valid_llm_json()])
        inspection = asyncio.run(
            pipeline.run_inspection(session, assistant.id, SAMPLE_DIALOGUE, "样例会话", client=client, cfg={})
        )
        assert inspection.total_score == 69
        assert inspection.is_yellow_alert is False
        assert inspection.turn_count == 8
        assert inspection.customer_profile == "焦虑型"
        assert inspection.template_snapshot_json
        detail = repository.get_inspection_detail(session, inspection.id)
        assert detail is not None
        assert "[客]" in detail.raw_dialogue
        assert len(client.calls) == 1

    def test_low_score_triggers_yellow(self, session, assistant):
        client = MockLLMClient([low_llm_json()])
        inspection = asyncio.run(
            pipeline.run_inspection(session, assistant.id, SAMPLE_DIALOGUE, client=client, cfg={})
        )
        assert inspection.is_yellow_alert is True
        assert inspection.total_score < 59

    def test_model_fake_score_overridden(self, session, assistant):
        # 模型输出 total_score=99 但维度合计只有 69 → 后端重算为 69
        import json

        data = json.loads(valid_llm_json())
        data["total_score"] = 99
        client = MockLLMClient([json.dumps(data, ensure_ascii=False)])
        inspection = asyncio.run(
            pipeline.run_inspection(session, assistant.id, SAMPLE_DIALOGUE, client=client, cfg={})
        )
        assert inspection.total_score == 69

    def test_bad_json_retries_then_succeeds(self, session, assistant):
        client = MockLLMClient(["这不是JSON", valid_llm_json()])
        inspection = asyncio.run(
            pipeline.run_inspection(session, assistant.id, SAMPLE_DIALOGUE, client=client, cfg={})
        )
        assert inspection.total_score == 69
        assert len(client.calls) == 2

    def test_llm_error_no_save(self, session, assistant):
        from backend.services.llm.base import LLMError

        client = MockLLMClient([LLMError("auth", "key 无效")])
        with pytest.raises(BizError) as excinfo:
            asyncio.run(
                pipeline.run_inspection(session, assistant.id, SAMPLE_DIALOGUE, client=client, cfg={})
            )
        assert excinfo.value.code == "auth"
        rows, _ = repository.list_inspections(session, assistant_id=assistant.id)
        assert rows == []  # 失败不落库

    def test_teacher_persona_injected_into_prompts(self, session, assistant):
        """老师人设注入：主调用用户提示词与 L3 改写用户提示词都携带 persona。"""
        from backend.db import repository as _repo

        _repo.update_assistant(session, assistant, assistant.name, assistant.template_type, "王老师，技术面风格，用「你」平视")
        client = MockLLMClient([valid_llm_json()])
        asyncio.run(pipeline.run_inspection(session, assistant.id, SAMPLE_DIALOGUE, client=client, cfg={}))
        assert "王老师" in client.calls[0]["user"]

    def test_red_alert_saved_with_inspection(self, session, assistant):
        """红灯一票否决：模型标红 → 落库并透传。"""
        client = MockLLMClient([valid_llm_json(red=True, red_reasons=["承诺收益：保准回本", "报点位：跌到12块买"])])
        inspection = asyncio.run(
            pipeline.run_inspection(session, assistant.id, SAMPLE_DIALOGUE, client=client, cfg={})
        )
        assert inspection.is_red_alert is True
        assert "保准回本" in inspection.red_alert_reasons_json

    def test_parse_error_400(self, session, assistant):
        client = MockLLMClient([valid_llm_json()])
        with pytest.raises(BizError) as excinfo:
            asyncio.run(
                pipeline.run_inspection(session, assistant.id, "完全没有角色标记的文本", client=client, cfg={})
            )
        assert excinfo.value.code == "parse_failed"
        assert len(client.calls) == 0  # 防呆硬错误不进模型

    def test_missing_assistant_404(self, session):
        client = MockLLMClient([valid_llm_json()])
        with pytest.raises(BizError) as excinfo:
            asyncio.run(
                pipeline.run_inspection(session, 9999, SAMPLE_DIALOGUE, client=client, cfg={})
            )
        assert excinfo.value.status_code == 404

    def test_split_fallback_works(self, session, assistant):
        """L1/L2 均输出坏 JSON，L3 拆分成功：评分一次 + 改写一次。"""
        from backend.services.pipeline import RewriteOnlySchema, ScoringOnlySchema
        import json as _json

        scoring_part = _json.loads(valid_llm_json())
        scoring_payload = {
            "total_score": scoring_part["total_score"],
            "is_red_alert": False,
            "red_alert_reasons": [],
            "is_yellow_alert": False,
            "yellow_alert_reasons": [],
            "d_scores": scoring_part["d_scores"],
            "s_scores": scoring_part["s_scores"],
        }
        rewrite_payload = {
            "highlight_dialogue": scoring_part["highlight_dialogue"],
            "improvement_suggestions": scoring_part["improvement_suggestions"],
        }
        client = MockLLMClient(
            [
                "坏JSON-1",
                "坏JSON-2",
                "坏JSON-3",
                "坏JSON-4",  # L1(3次) + L2(1次) 全失败
                _json.dumps(scoring_payload, ensure_ascii=False),  # L3-A
                _json.dumps(rewrite_payload, ensure_ascii=False),  # L3-B
            ]
        )
        inspection = asyncio.run(
            pipeline.run_inspection(session, assistant.id, SAMPLE_DIALOGUE, client=client, cfg={})
        )
        assert inspection.total_score == 69
        detail = repository.get_inspection_detail(session, inspection.id)
        assert detail.highlight_dialogue_json

    def test_evaluatee_injected_and_saved(self, session, assistant):
        """评估对象锁定：prompt 注入「本次评估对象」且落库。"""
        client = MockLLMClient([valid_llm_json()])
        dialogue = "[客服A] 您好，有什么可以帮您？\n[客户] 我的基金亏了。\n[客服B] 我来帮您查询。\n[客户] 好的。"
        asyncio.run(
            pipeline.run_inspection(session, assistant.id, dialogue, client=client, cfg={}, evaluatee="客服B")
        )
        assert "本次评估对象：客服B" in client.calls[0]["user"]
        inspection = repository.list_inspections(session, assistant_id=assistant.id)[0][0]
        assert inspection.evaluatee == "客服B"

    def test_evaluatee_fallback_to_first_speaker(self, session, assistant):
        """前端未传评估对象 → 后端按唯一/首个助理推导（兜底不阻塞）。"""
        client = MockLLMClient([valid_llm_json()])
        dialogue = "[客服A] 您好。\n[客户] 你好。\n[客服B] 请问怎么称呼？"
        inspection = asyncio.run(pipeline.run_inspection(session, assistant.id, dialogue, client=client, cfg={}))
        assert inspection.evaluatee == "客服A"

    def test_na_saved_with_inspection(self, session, assistant):
        """N/A 豁免落库：na_dims_json / effective_max / 折算总分 / evaluatee。"""
        import json as _json

        data = _json.loads(valid_llm_json())
        data["d_scores"]["d2_profile_match"] = {"score": None, "na_reason": "客户未表达情绪"}
        client = MockLLMClient([_json.dumps(data, ensure_ascii=False)])
        inspection = asyncio.run(pipeline.run_inspection(session, assistant.id, SAMPLE_DIALOGUE, client=client, cfg={}))
        assert inspection.effective_max == 85  # 100 − D2 的 15 分
        na = _json.loads(inspection.na_dims_json)
        assert na[0]["key"] == "d2"
        # 原始维度合计 57 → 57/85 → 67
        assert inspection.total_score == round(57 / 85 * 100)
        assert inspection.evaluatee == "助理A"

    def test_all_fail_raises_llm_failed(self, session, assistant):
        client = MockLLMClient(["坏JSON"] * 8)
        with pytest.raises(BizError) as excinfo:
            asyncio.run(
                pipeline.run_inspection(session, assistant.id, SAMPLE_DIALOGUE, client=client, cfg={})
            )
        assert excinfo.value.code == "llm_failed"
        rows, _ = repository.list_inspections(session, assistant_id=assistant.id)
        assert rows == []
