import json

import pytest

from backend.config import DEFAULT_TEMPLATES
from backend.schemas.inspection import LLMResultSchema
from backend.services.prompts import build_scoring_only_system, build_system_prompt
from backend.services.scoring import (
    apply_guardrails,
    render_rulebook,
    validate_template_config,
)


def _result_from_json(text: str) -> LLMResultSchema:
    return LLMResultSchema.model_validate(json.loads(text))


def _template() -> dict:
    return DEFAULT_TEMPLATES["standard"]


def _base_result(total=69, red=False, red_reasons=None) -> dict:
    """v2 格式基础输出：子项对象化 + analysis + 红灯字段。"""
    return {
        "total_score": total,
        "is_red_alert": red,
        "red_alert_reasons": red_reasons or [],
        "is_yellow_alert": False,
        "yellow_alert_reasons": [],
        "d_scores": {
            "d1_emotion_change": {"score": 10},
            "d2_profile_match": {"score": 15},
            "d3_problem_match": {"score": 15},
            "d4_expectation_exceed": {"score": 15},
        },
        "s_scores": {
            "s1_emotion_stabilize": {"sub_items": {"empathy": {"score": 4}, "customized": {"score": 5}, "direct": {"score": 4}, "no_conflict": {"score": 5}, "vent_guide": {"score": 2}}},
            "s2_problem_closure": {"sub_items": {"completeness": {"score": 4}, "structure": {"score": 4}, "next_step": {"score": 3}, "follow_up": {"score": 4}}},
            "s3_professional_supply": {"sub_items": {"logic": {"score": 4}, "explain_why": {"score": 3}, "decision_ownership": {"score": 3}}},
        },
    }


class TestRulebook:
    def test_contains_weights_and_fuse(self):
        book = render_rulebook(_template())
        assert "D端 55 分 + S端 45 分" in book
        assert "59" in book
        assert "共情 4 分" in book
        assert "黄灯熔断机制" in book


class TestScoringMechanismText:
    """打分机制文字版（业务方提供）已新增注入主提示词与 L3 评分提示词。"""

    def test_injected_into_system_prompt(self):
        sys_prompt = build_system_prompt(render_rulebook(_template()))
        assert "业务打分机制文字版（补充参照）" in sys_prompt
        assert "S1 情绪维度（满分 23分）" in sys_prompt
        assert "胖东来式体验" in sys_prompt
        assert "情绪事故，黄灯及以下" in sys_prompt
        assert "S2-1 回应完整性" in sys_prompt
        assert "D2-1 情绪识别" in sys_prompt
        assert "D4-1 冰山下问" in sys_prompt

    def test_injected_into_scoring_only_prompt(self):
        sys_prompt = build_scoring_only_system(render_rulebook(_template()))
        assert "业务打分机制文字版（补充参照）" in sys_prompt
        assert "S1 情绪维度（满分 23分）" in sys_prompt
        assert "胖东来式体验" in sys_prompt

    def test_existing_content_preserved(self):
        # 新增不覆盖：原有三道红线 / 输出协议 / Bad Case 教学都在
        sys_prompt = build_system_prompt(render_rulebook(_template()))
        assert "三道红线" in sys_prompt
        assert "is_red_alert" in sys_prompt
        assert "这只票明天肯定反弹" in sys_prompt
        assert "生成前自检" not in sys_prompt  # 改写自检属于 L3 改写提示词，不在主评分提示词


class TestGuardrails:
    def test_total_recalculated(self):
        result = _result_from_json(
            '{"total_score": 100, "is_yellow_alert": false, "yellow_alert_reasons": [],'
            ' "d_scores": {"d1_emotion_change": {"score": 10}, "d2_profile_match": {"score": 15},'
            ' "d3_problem_match": {"score": 15}, "d4_expectation_exceed": {"score": 15}},'
            ' "s_scores": {"s1_emotion_stabilize": {"score": 20, "sub_items": {"empathy": 4, "customized": 5, "direct": 4, "no_conflict": 5, "vent_guide": 2}},'
            ' "s2_problem_closure": {"score": 15, "sub_items": {"completeness": 4, "structure": 4, "next_step": 3, "follow_up": 4}},'
            ' "s3_professional_supply": {"score": 10, "sub_items": {"logic": 4, "explain_why": 3, "decision_ownership": 3}}}}'
        )
        apply_guardrails(result, _template(), turn_count=10)
        assert result.total_score == 100
        assert not result.is_yellow_alert

    def test_model_wrong_total_overridden(self):
        result = _result_from_json(
            '{"total_score": 30, "is_yellow_alert": false, "yellow_alert_reasons": [],'
            ' "d_scores": {"d1_emotion_change": {"score": 9}, "d2_profile_match": {"score": 14},'
            ' "d3_problem_match": {"score": 14}, "d4_expectation_exceed": {"score": 14}},'
            ' "s_scores": {"s1_emotion_stabilize": {"score": 18, "sub_items": {"empathy": 4, "customized": 4, "direct": 4, "no_conflict": 4, "vent_guide": 2}},'
            ' "s2_problem_closure": {"score": 13, "sub_items": {"completeness": 4, "structure": 3, "next_step": 2, "follow_up": 4}},'
            ' "s3_professional_supply": {"score": 9, "sub_items": {"logic": 3, "explain_why": 3, "decision_ownership": 3}}}}'
        )
        apply_guardrails(result, _template(), turn_count=10)
        # 模型谎报 30 分，实际维度合计 91 → 后端重算为 91，不触发黄灯
        assert result.total_score == 9 + 14 + 14 + 14 + 18 + 13 + 9
        assert result.is_yellow_alert is False

    def test_yellow_fuse_forced_and_reasons_filled(self):
        result = _result_from_json(
            '{"total_score": 90, "is_yellow_alert": false, "yellow_alert_reasons": [],'
            ' "d_scores": {"d1_emotion_change": {"score": 5}, "d2_profile_match": {"score": 6},'
            ' "d3_problem_match": {"score": 6}, "d4_expectation_exceed": {"score": 6}},'
            ' "s_scores": {"s1_emotion_stabilize": {"score": 8, "sub_items": {"empathy": 2, "customized": 2, "direct": 1, "no_conflict": 2, "vent_guide": 1}},'
            ' "s2_problem_closure": {"score": 6, "sub_items": {"completeness": 2, "structure": 1, "next_step": 1, "follow_up": 2}},'
            ' "s3_professional_supply": {"score": 4, "sub_items": {"logic": 2, "explain_why": 1, "decision_ownership": 1}}}}'
        )
        # 模型为避黄灯虚报 90 分：实际 5+6+6+6+8+6+4=41 < 59 → 强制黄灯并补 reasons
        apply_guardrails(result, _template(), turn_count=10)
        assert result.total_score == 41
        assert result.is_yellow_alert is True
        assert result.yellow_alert_reasons

    def test_dimension_score_equals_sub_sum(self):
        # 算术剥离：模型谎报 S1 维度分 10，后端按子项求和强制覆盖为 20
        data = _base_result()
        data["d_scores"] = {
            "d1_emotion_change": {"score": 8},
            "d2_profile_match": {"score": 12},
            "d3_problem_match": {"score": 9},
            "d4_expectation_exceed": {"score": 7},
        }
        data["s_scores"] = {
            "s1_emotion_stabilize": {"score": 10, "sub_items": {"empathy": {"analysis": "a", "score": 4}, "customized": {"analysis": "b", "score": 5}, "direct": {"analysis": "c", "score": 4}, "no_conflict": {"analysis": "d", "score": 5}, "vent_guide": {"analysis": "e", "score": 2}}},
            "s2_problem_closure": {"score": 11, "sub_items": {"completeness": {"analysis": "f", "score": 3}, "structure": {"analysis": "g", "score": 3}, "next_step": {"analysis": "h", "score": 2}, "follow_up": {"analysis": "i", "score": 3}}},
            "s3_professional_supply": {"score": 7, "sub_items": {"logic": {"analysis": "j", "score": 3}, "explain_why": {"analysis": "k", "score": 2}, "decision_ownership": {"analysis": "l", "score": 2}}},
        }
        result = _result_from_json(json.dumps(data, ensure_ascii=False))
        apply_guardrails(result, _template(), turn_count=10)
        subs = result.s_scores.s1_emotion_stabilize.sub_items
        # 维度分 = Σ子项（模型给的 10 被覆盖为 20）
        assert result.s_scores.s1_emotion_stabilize.score == 4 + 5 + 4 + 5 + 2
        assert result.s_scores.s2_problem_closure.score == 3 + 3 + 2 + 3
        assert result.s_scores.s3_professional_supply.score == 3 + 2 + 2
        assert subs.empathy.score == 4
        assert subs.empathy.analysis == "a"  # 思维链文本原样保留

    def test_legacy_int_sub_items_still_accepted(self):
        # 兼容旧格式：子项直接给整数 → 自动补空 analysis
        result = _result_from_json(
            '{"total_score": 69, "is_yellow_alert": false, "yellow_alert_reasons": [],'
            ' "d_scores": {"d1_emotion_change": {"score": 8}, "d2_profile_match": {"score": 12},'
            ' "d3_problem_match": {"score": 9}, "d4_expectation_exceed": {"score": 7}},'
            ' "s_scores": {"s1_emotion_stabilize": {"sub_items": {"empathy": 4, "customized": 4, "direct": 3, "no_conflict": 4, "vent_guide": 0}},'
            ' "s2_problem_closure": {"sub_items": {"completeness": 3, "structure": 3, "next_step": 2, "follow_up": 3}},'
            ' "s3_professional_supply": {"sub_items": {"logic": 3, "explain_why": 2, "decision_ownership": 2}}}}'
        )
        apply_guardrails(result, _template(), turn_count=10)
        assert result.s_scores.s1_emotion_stabilize.score == 4 + 4 + 3 + 4 + 0
        assert result.s_scores.s1_emotion_stabilize.sub_items.empathy.analysis == ""

    def test_red_alert_reasons_filled(self):
        # 红灯一票否决：触发但 reasons 为空 → 后端补默认原因
        data = _base_result(red=True, red_reasons=[])
        result = _result_from_json(json.dumps(data, ensure_ascii=False))
        apply_guardrails(result, _template(), turn_count=10)
        assert result.is_red_alert is True
        assert result.red_alert_reasons  # 自动补全
        assert not result.is_yellow_alert  # 红灯独立于黄灯，总分 100 不触发黄灯

    def test_red_alert_independent_of_total(self):
        # 高总分也不掩盖红灯；模型给的原因被保留并裁剪到 3 条
        data = _base_result(red=True, red_reasons=["承诺收益", "报点位", "代客决定", "多余"])
        result = _result_from_json(json.dumps(data, ensure_ascii=False))
        apply_guardrails(result, _template(), turn_count=10)
        assert result.is_red_alert is True
        assert len(result.red_alert_reasons) == 3

    def test_score_clamped_to_max(self):
        result = _result_from_json(
            '{"total_score": 100, "is_yellow_alert": false, "yellow_alert_reasons": [],'
            ' "d_scores": {"d1_emotion_change": {"score": 99}, "d2_profile_match": {"score": 15},'
            ' "d3_problem_match": {"score": 15}, "d4_expectation_exceed": {"score": 15}},'
            ' "s_scores": {"s1_emotion_stabilize": {"score": 20, "sub_items": {"empathy": 4, "customized": 5, "direct": 4, "no_conflict": 5, "vent_guide": 2}},'
            ' "s2_problem_closure": {"score": 15, "sub_items": {"completeness": 4, "structure": 4, "next_step": 3, "follow_up": 4}},'
            ' "s3_professional_supply": {"score": 10, "sub_items": {"logic": 4, "explain_why": 3, "decision_ownership": 3}}}}'
        )
        apply_guardrails(result, _template(), turn_count=10)
        assert result.d_scores.d1_emotion_change.score == 10  # 99 → 钳制到满分 10

    def test_highlight_filtered_and_sorted(self):
        result = _result_from_json(
            '{"total_score": 69, "is_yellow_alert": false, "yellow_alert_reasons": [],'
            ' "d_scores": {"d1_emotion_change": {"score": 8}, "d2_profile_match": {"score": 12},'
            ' "d3_problem_match": {"score": 9}, "d4_expectation_exceed": {"score": 7}},'
            ' "s_scores": {"s1_emotion_stabilize": {"score": 15, "sub_items": {"empathy": 4, "customized": 4, "direct": 3, "no_conflict": 4, "vent_guide": 0}},'
            ' "s2_problem_closure": {"score": 11, "sub_items": {"completeness": 3, "structure": 3, "next_step": 2, "follow_up": 3}},'
            ' "s3_professional_supply": {"score": 7, "sub_items": {"logic": 3, "explain_why": 2, "decision_ownership": 2}}},'
            ' "highlight_dialogue": ['
            '  {"turn": 99, "role": "助", "original_text": "越界", "issue_type": "X", "ai_rewrite": "Y"},'
            '  {"turn": 5, "role": "助理", "original_text": "A", "issue_type": "S1-1", "ai_rewrite": "B"},'
            '  {"turn": 2, "role": "", "original_text": "C", "issue_type": "S2-3", "ai_rewrite": "D"}'
            ']}'
        )
        apply_guardrails(result, _template(), turn_count=10)
        assert [h.turn for h in result.highlight_dialogue] == [2, 5]
        assert result.highlight_dialogue[0].role == "助"  # 空角色补默认

    def test_suggestions_trimmed(self):
        result = _result_from_json(
            '{"total_score": 69, "is_yellow_alert": false, "yellow_alert_reasons": [],'
            ' "d_scores": {"d1_emotion_change": {"score": 8}, "d2_profile_match": {"score": 12},'
            ' "d3_problem_match": {"score": 9}, "d4_expectation_exceed": {"score": 7}},'
            ' "s_scores": {"s1_emotion_stabilize": {"score": 15, "sub_items": {"empathy": 4, "customized": 4, "direct": 3, "no_conflict": 4, "vent_guide": 0}},'
            ' "s2_problem_closure": {"score": 11, "sub_items": {"completeness": 3, "structure": 3, "next_step": 2, "follow_up": 3}},'
            ' "s3_professional_supply": {"score": 7, "sub_items": {"logic": 3, "explain_why": 2, "decision_ownership": 2}}},'
            ' "improvement_suggestions": ["a", "b", "c", "d", "e"]}'
        )
        apply_guardrails(result, _template(), turn_count=10)
        assert len(result.improvement_suggestions) == 3

    # ---------- N/A 豁免（防呆：无法判定 → null + 动态分母折算） ----------

    def test_na_dimension_deducted_from_denominator(self):
        # D2 无法判定（客户未表达情绪）→ 豁免 15 分：分母 85，其余满分 → 折算 100
        data = _base_result()
        data["d_scores"]["d2_profile_match"] = {"score": None, "na_reason": "客户未表达情绪"}
        result = _result_from_json(json.dumps(data, ensure_ascii=False))
        apply_guardrails(result, _template(), turn_count=10)
        assert result.effective_max == 85
        assert result.na_dims == [{"key": "d2", "name": "画像匹配", "reason": "客户未表达情绪", "max": 15}]
        assert result.d_scores.d2_profile_match.score is None  # 原样保留，严禁被钳制为 0
        assert result.total_score == 100
        assert not result.is_yellow_alert

    def test_na_prorated_score(self):
        # 豁免 D2（分母 85）后按得分率折算：61/85 → 72（round(71.76)）
        data = _base_result()
        data["d_scores"] = {
            "d1_emotion_change": {"score": 7},
            "d2_profile_match": {"score": None, "na_reason": "无情绪对话"},
            "d3_problem_match": {"score": 10},
            "d4_expectation_exceed": {"score": 9},
        }
        data["s_scores"] = {
            "s1_emotion_stabilize": {"sub_items": {"empathy": 4, "customized": 3, "direct": 2, "no_conflict": 4, "vent_guide": 2}},
            "s2_problem_closure": {"sub_items": {"completeness": 3, "structure": 3, "next_step": 2, "follow_up": 4}},
            "s3_professional_supply": {"sub_items": {"logic": 3, "explain_why": 2, "decision_ownership": 3}},
        }
        result = _result_from_json(json.dumps(data, ensure_ascii=False))
        apply_guardrails(result, _template(), turn_count=10)
        # 有效得分 = (7+10+9) + 15 + 12 + 8 = 61 → 61/85 ≈ 71.8 → 72
        assert result.effective_max == 85
        assert result.total_score == 72
        assert not result.is_yellow_alert

    def test_na_yellow_by_prorated_score(self):
        # 豁免 D1（分母 90）后得分率仍低 → 黄灯按折算后百分制判断
        data = _base_result()
        data["d_scores"] = {
            "d1_emotion_change": {"score": None, "na_reason": "无情绪表达"},
            "d2_profile_match": {"score": 15},
            "d3_problem_match": {"score": 15},
            "d4_expectation_exceed": {"score": 15},
        }
        data["s_scores"] = {
            "s1_emotion_stabilize": {"sub_items": {"empathy": 1, "customized": 1, "direct": 0, "no_conflict": 0, "vent_guide": 0}},
            "s2_problem_closure": {"sub_items": {"completeness": 1, "structure": 0, "next_step": 0, "follow_up": 1}},
            "s3_professional_supply": {"sub_items": {"logic": 1, "explain_why": 0, "decision_ownership": 0}},
        }
        result = _result_from_json(json.dumps(data, ensure_ascii=False))
        apply_guardrails(result, _template(), turn_count=10)
        # 有效得分 = 45 + 2 + 2 + 1 = 50 → 50/90 ≈ 55.6 → 56 < 59 → 黄灯
        assert result.effective_max == 90
        assert result.total_score == round(50 / 90 * 100)
        assert result.is_yellow_alert is True
        assert result.yellow_alert_reasons
        assert all("情绪转化" not in r for r in result.yellow_alert_reasons)  # N/A 维度不进失分统计

    def test_na_reason_required(self):
        # score=null 但没给 na_reason → schema 校验失败（触发既有 json_guard 重试）
        with pytest.raises(ValueError):
            _result_from_json(
                '{"total_score": 0, "is_red_alert": false, "red_alert_reasons": [],'
                ' "is_yellow_alert": false, "yellow_alert_reasons": [],'
                ' "d_scores": {"d1_emotion_change": {"score": null}, "d2_profile_match": {"score": 15},'
                ' "d3_problem_match": {"score": 15}, "d4_expectation_exceed": {"score": 15}},'
                ' "s_scores": {"s1_emotion_stabilize": {"sub_items": {"empathy": 4, "customized": 5, "direct": 4, "no_conflict": 5, "vent_guide": 2}},'
                ' "s2_problem_closure": {"sub_items": {"completeness": 4, "structure": 4, "next_step": 3, "follow_up": 4}},'
                ' "s3_professional_supply": {"sub_items": {"logic": 4, "explain_why": 3, "decision_ownership": 3}}}}'
            )

    def test_na_limit_two_max(self):
        # 单次最多豁免 2 个维度；3 个 N/A → 校验失败（防模型全标 N/A 拿高分）
        with pytest.raises(ValueError):
            _result_from_json(
                '{"total_score": 0, "is_red_alert": false, "red_alert_reasons": [],'
                ' "is_yellow_alert": false, "yellow_alert_reasons": [],'
                ' "d_scores": {"d1_emotion_change": {"score": null, "na_reason": "a"},'
                ' "d2_profile_match": {"score": null, "na_reason": "b"},'
                ' "d3_problem_match": {"score": null, "na_reason": "c"},'
                ' "d4_expectation_exceed": {"score": 15}},'
                ' "s_scores": {"s1_emotion_stabilize": {"sub_items": {"empathy": 4, "customized": 5, "direct": 4, "no_conflict": 5, "vent_guide": 2}},'
                ' "s2_problem_closure": {"sub_items": {"completeness": 4, "structure": 4, "next_step": 3, "follow_up": 4}},'
                ' "s3_professional_supply": {"sub_items": {"logic": 4, "explain_why": 3, "decision_ownership": 3}}}}'
            )

    def test_s_dimension_na_exempts_whole_dimension(self):
        # S 维度 N/A → 子项一并豁免（分数忽略），分母扣该维度满分 20
        data = _base_result()
        data["s_scores"]["s1_emotion_stabilize"] = {
            "score": None,
            "na_reason": "客户未流露情绪，无共情对话可评",
            "sub_items": {"empathy": 4, "customized": 5, "direct": 4, "no_conflict": 5, "vent_guide": 2},
        }
        result = _result_from_json(json.dumps(data, ensure_ascii=False))
        apply_guardrails(result, _template(), turn_count=10)
        assert result.effective_max == 80  # 100 − s1 的 20 分
        na = {nd["key"]: nd for nd in result.na_dims}
        assert na["s1"]["max"] == 20
        assert result.s_scores.s1_emotion_stabilize.score is None
        assert result.total_score == 100  # 其余满分 80/80


class TestTemplateValidation:
    def test_default_templates_valid(self):
        for ttype, config in DEFAULT_TEMPLATES.items():
            assert validate_template_config(config) == [], f"{ttype} 默认模板不合法"

    def test_broken_sums_detected(self):
        config = json.loads(json.dumps(DEFAULT_TEMPLATES["standard"]))
        config["d"]["d1"]["max"] = 9  # D 合计 54 ≠ 55
        errors = validate_template_config(config)
        assert any("D 端" in e for e in errors)

    def test_sub_item_sum_mismatch_detected(self):
        config = json.loads(json.dumps(DEFAULT_TEMPLATES["standard"]))
        config["s"]["s1"]["sub_items"]["empathy"]["max"] = 9  # S1 子项合计 25 ≠ 20
        errors = validate_template_config(config)
        assert any("s1 子项" in e for e in errors)
