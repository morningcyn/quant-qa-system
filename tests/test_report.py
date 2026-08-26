# 统计聚合回归：Top3 失分必须兼容 v2 子项对象化（{"analysis", "score"}）与旧 int 格式
import json

import pytest

from backend.config import DEFAULT_TEMPLATES
from backend.db import repository
from backend.services.report import top3_loss

SNAPSHOT = DEFAULT_TEMPLATES["standard"]


@pytest.fixture()
def assistant(session):
    return repository.create_assistant(session, "张三", "E001", "standard")


S_SUBS = {
    "s1_emotion_stabilize": {"score": 15, "sub_items": {
        "empathy": {"analysis": "有承接", "score": 3},       # max 4 → loss 1
        "customized": {"analysis": "针对持仓", "score": 4},  # max 5 → loss 1
        "direct": {"analysis": "正面回应", "score": 4},      # max 4 → loss 0
        "no_conflict": {"analysis": "未争辩", "score": 3},   # max 5 → loss 2
        "vent_guide": {"analysis": "未引导", "score": 1},    # max 2 → loss 1
    }},
    "s2_problem_closure": {"score": 12, "sub_items": {
        "completeness": {"analysis": "a", "score": 3},
        "structure": {"analysis": "b", "score": 4},
        "next_step": {"analysis": "c", "score": 3},
        "follow_up": {"analysis": "d", "score": 2},
    }},
    "s3_professional_supply": {"score": 8, "sub_items": {
        "logic": {"analysis": "e", "score": 4},
        "explain_why": {"analysis": "f", "score": 3},
        "decision_ownership": {"analysis": "g", "score": 1},  # max 3 → loss 2
    }},
}


def _save(session, assistant_id, sub_items):
    repository.save_inspection(
        session, assistant_id, "Top3回归", total_score=69,
        is_yellow_alert=False, yellow_alert_reasons=[],
        template_snapshot=SNAPSHOT, turn_count=8, customer_profile="焦虑型",
        raw_dialogue="[客] hi\n[助] hi",
        d_scores={"d1_emotion_change": {"analysis": "a", "score": 8},
                  "d2_profile_match": {"analysis": "b", "score": 12},
                  "d3_problem_match": {"analysis": "c", "score": 9},
                  "d4_expectation_exceed": {"analysis": "d", "score": 7}},
        s_scores=sub_items,
        highlight_dialogue=[], suggestions=[],
    )


def test_top3_loss_with_object_sub_items(session, assistant):
    """v2 子项对象化：不得因 dict 与 int 相减而 500，失分按 score 字段计算。"""
    _save(session, assistant.id, S_SUBS)
    result = top3_loss(session, assistant.id, days=30)
    subs = {s["key"]: s for s in result["sub_items"]}
    # 失分最高的 3 条子项（loss=2）：no_conflict(5-3)、decision_ownership(3-1)、follow_up(4-2)
    assert len(subs) == 3
    assert subs["s1.no_conflict"]["loss_total"] == 2
    assert subs["s3.decision_ownership"]["loss_total"] == 2
    assert subs["s2.follow_up"]["loss_total"] == 2
    assert subs["s1.no_conflict"]["avg_score"] == 3.0
    assert subs["s1.no_conflict"]["occurrence_count"] == 1
    # 维度级失分不受子项对象化影响：Top3 为 d4(15-7=8)、d3(15-9=6)、s1(20-15=5)
    dims = {d["key"]: d for d in result["dimensions"]}
    assert len(dims) == 3
    assert dims["d4"]["loss_total"] == 8
    assert dims["d3"]["loss_total"] == 6
    assert dims["s1"]["loss_total"] == 5


def test_top3_loss_legacy_int_sub_items(session, assistant):
    """旧 v1 记录：子项直接 int → 依然正常聚合（向后兼容）。"""
    legacy = json.loads(json.dumps(S_SUBS))
    for dim in legacy.values():
        for k in dim["sub_items"]:
            dim["sub_items"][k] = dim["sub_items"][k]["score"]  # 还原为 int
    _save(session, assistant.id, legacy)
    result = top3_loss(session, assistant.id, days=30)
    subs = {s["key"]: s for s in result["sub_items"]}
    assert subs["s1.no_conflict"]["loss_total"] == 2
    assert subs["s3.decision_ownership"]["loss_total"] == 2


def test_top3_loss_empty_db(session, assistant):
    result = top3_loss(session, assistant.id, days=30)
    assert result["dimensions"] == []
    assert result["sub_items"] == []


def test_build_report_view_na_fields(session, assistant):
    """报告视图透传评估对象与 N/A 折算信息；effective_score 不含豁免维度得分。"""
    from backend.services.report import build_report_view

    repository.save_inspection(
        session, assistant.id, "N/A报告", total_score=72,
        is_yellow_alert=False, yellow_alert_reasons=[],
        template_snapshot=SNAPSHOT, turn_count=4, customer_profile=None,
        raw_dialogue="[客] hi\n[助] hi",
        d_scores={"d1_emotion_change": {"score": 7},
                  "d2_profile_match": {"score": None, "na_reason": "无情绪信息"},
                  "d3_problem_match": {"score": 10},
                  "d4_expectation_exceed": {"score": 9}},
        s_scores={"s1_emotion_stabilize": {"score": 15, "sub_items": {}},
                  "s2_problem_closure": {"score": 12, "sub_items": {}},
                  "s3_professional_supply": {"score": 8, "sub_items": {}}},
        highlight_dialogue=[], suggestions=[],
        evaluatee="助理A",
        na_dims=[{"key": "d2", "name": "画像匹配", "reason": "无情绪信息", "max": 15}],
        effective_max=85,
    )
    inspection = repository.list_inspections(session, assistant_id=assistant.id)[0][0]
    view = build_report_view(session, inspection)
    assert view["evaluatee"] == "助理A"
    assert view["na_dims"][0]["key"] == "d2"
    assert view["effective_max"] == 85
    assert view["effective_score"] == 7 + 10 + 9 + 15 + 12 + 8  # 豁免维度得分不计入


def test_top3_loss_skips_na_dimension(session, assistant):
    """Top3 失分统计跳过 N/A 豁免维度。"""
    repository.save_inspection(
        session, assistant.id, "N/A Top3", total_score=72,
        is_yellow_alert=False, yellow_alert_reasons=[],
        template_snapshot=SNAPSHOT, turn_count=4, customer_profile=None,
        raw_dialogue="[客] hi\n[助] hi",
        d_scores={"d1_emotion_change": {"score": 10},
                  "d2_profile_match": {"score": None, "na_reason": "无情绪信息"},
                  "d3_problem_match": {"score": 15},
                  "d4_expectation_exceed": {"score": 15}},
        s_scores=S_SUBS,
        highlight_dialogue=[], suggestions=[],
        na_dims=[{"key": "d2", "name": "画像匹配", "reason": "无情绪信息", "max": 15}],
        effective_max=85,
    )
    result = top3_loss(session, assistant.id, days=30)
    dims = {d["key"]: d for d in result["dimensions"]}
    assert "d2" not in dims
