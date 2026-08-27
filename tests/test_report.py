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


# ---------- derive_strengths（多人质检对比看板「可借鉴」） ----------

from backend.services.report import derive_strengths  # noqa: E402


def _strength_report(**overrides):
    """最小可用的报告视图字典（derive_strengths 只读这些键）。"""
    base = {
        "total_score": 72,
        "is_red_alert": False,
        "is_yellow_alert": False,
        "highlight_dialogue": [],
        "turn_count": 6,
        "reply_count": 5,
        "na_dims": [],
        "template_snapshot": SNAPSHOT,
        "d_scores": {
            "d1_emotion_change": {"score": 6},   # 6/10
            "d2_profile_match": {"score": 15},   # 15/15
            "d3_problem_match": {"score": 15},   # 15/15
            "d4_expectation_exceed": {"score": 9},  # 9/15
        },
        "s_scores": {
            "s1_emotion_stabilize": {"score": 16},  # 16/20
            "s2_problem_closure": {"score": 9},     # 9/15 → 子项检查
            "s3_professional_supply": {"score": 4},  # 4/10
        },
    }
    base.update(overrides)
    return base


def test_derive_strengths_high_ratio_dims():
    """得分率 ≥80% 的维度成为亮点，按得分率降序且带维度名。"""
    s = derive_strengths(_strength_report())
    assert s[0] == "画像匹配（15/15 分，得分率 100%）"
    assert "诉求穿透（15/15 分，得分率 100%）" in s
    assert "情绪维度（16/20 分，得分率 80%）" in s
    assert len(s) == 3  # 上限 3 条，候选充足不补足


def test_derive_strengths_short_key_compat():
    """短键存储（d1…）同样兼容。"""
    r = _strength_report(
        d_scores={"d1": {"score": 10}, "d2": {"score": 15}, "d3": {"score": 6}, "d4": {"score": 4}},
        s_scores={"s1": {"score": 20}, "s2": {"score": 5}, "s3": {"score": 3}},
    )
    s = derive_strengths(r)
    assert "情绪转化（10/10 分，得分率 100%）" in s
    assert "画像匹配（15/15 分，得分率 100%）" in s
    assert "情绪维度（20/20 分，得分率 100%）" in s


def test_derive_strengths_comment_truncated():
    """维度 comment 非空且超长时截断补"…"。"""
    r = _strength_report(d_scores={
        "d1_emotion_change": {"score": 10, "comment": "非常好的共情表达" * 10},
        "d2_profile_match": {"score": 6},
        "d3_problem_match": {"score": 6},
        "d4_expectation_exceed": {"score": 4},
    })
    s = derive_strengths(r)
    assert any(x.startswith("情绪转化（10/10 分，得分率 100%）：") and x.endswith("…") for x in s)


def test_derive_strengths_na_skipped_and_fallback():
    """N/A 维度跳过；候选不足时按确定性规则补足（合规健康/话术规范/持续接待）。"""
    r = _strength_report(
        d_scores={"d1_emotion_change": {"score": 5},
                  "d2_profile_match": {"score": 6},
                  "d3_problem_match": {"score": 5},
                  "d4_expectation_exceed": {"score": 4}},
        s_scores={"s1_emotion_stabilize": {"score": 8},
                  "s2_problem_closure": {"score": 5},
                  "s3_professional_supply": {"score": 3}},
        na_dims=[{"key": "d1", "name": "情绪转化"}],
        highlight_dialogue=[{"turn": 2, "issue_type": "x", "original_text": "y", "ai_rewrite": "z"}],
    )
    s = derive_strengths(r)
    assert not any("情绪转化" in x for x in s)  # N/A 不进候选
    assert "整体服务达标：无红灯违规，总分 72 分" in s  # 补足条
    assert "全程持续接待：共 5 次回复，服务衔接完整" in s  # highlight 非空不补话术规范


def test_derive_strengths_sub_item_fallback():
    """S 维度整体未达标时，高分子项成为亮点。"""
    r = _strength_report(s_scores={"s1_emotion_stabilize": {"score": 8, "sub_items": {
        "empathy": {"score": 4},       # 4/4 → 亮点
        "customized": {"score": 1},    # 1/5
        "direct": {"score": 1},
        "no_conflict": {"score": 1},
        "vent_guide": {"score": 1},
    }}})
    s = derive_strengths(r)
    assert any("情绪维度·共情回应" in x or "情绪维度·" in x for x in s)


def test_derive_strengths_d4_action_special():
    """D4 高分且含预判/掌控感动作时，用动作型措辞代替泛化措辞。"""
    r = _strength_report(d_scores={
        "d1_emotion_change": {"score": 5},
        "d2_profile_match": {"score": 15, "derived_question": 0},
        "d3_problem_match": {"score": 5},
        "d4_expectation_exceed": {"score": 15, "derived_question": 2, "control_given": 1},
    })
    s = derive_strengths(r)
    assert any("预期超越：预判衍生问题 2 个、掌控感动作 1 个" in x for x in s)


def test_derive_strengths_empty_report():
    """无任何维度数据（报告异常/字段缺失）→ 兜底补足且不抛异常。"""
    s = derive_strengths({})
    assert isinstance(s, list) and len(s) >= 1
