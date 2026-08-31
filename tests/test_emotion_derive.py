# 客户情绪确定性派生：emotion_change 全表驱动 / 助理效果归属 / 会话摘要
# 全部为纯规则计算，不依赖 LLM —— 这是情绪模块的可复现核心。
import pytest

from backend.services.emotion.derive import (
    EMOTION_RANK,
    LOW_CONFIDENCE_THRESHOLD,
    NEGATIVE_EMOTIONS,
    build_summary,
    emotion_change,
)
from backend.services.multiparser import MultiMessage


def mk(role, turn_no, text="x", assistant_id=None, canonical_name=None):
    """构造 MultiMessage（canonical_name：客户侧="客户"，助侧=规范名）。"""
    if role == "客":
        speaker, canonical = "客户", "客户"
    else:
        speaker = canonical_name or f"助理{turn_no}"
        canonical = canonical_name or speaker
    return MultiMessage(
        turn_no=turn_no,
        role=role,
        speaker=speaker,
        canonical_name=canonical,
        text=text,
        timestamp=None,
        assistant_id=assistant_id,
        raw_line="",
    )


def item(turn_no, emotion, intensity=3, confidence=0.9, trigger="行情波动", **over):
    d = {
        "turn_no": turn_no,
        "emotion": emotion,
        "intensity": intensity,
        "confidence": confidence,
        "trigger": trigger,
        "evidence": "原话",
        "synthesized": False,
        "evidence_adjusted": False,
    }
    d.update(over)
    return d


# ---------- emotion_change：用户示例 + 全表驱动 ----------

class TestEmotionChange:
    def test_user_examples(self):
        """用户明确给出的示例方向。"""
        assert emotion_change(item(1, "中性"), item(2, "担忧")) == "worsened"
        assert emotion_change(item(1, "焦虑"), item(2, "中性")) == "improved"
        assert emotion_change(item(1, "担忧"), item(2, "积极/认可")) == "improved"
        assert emotion_change(item(1, "中性"), item(2, "中性")) == "unchanged"

    def test_full_rank_table(self):
        """8×8 全表：rank 高（更负面）→ worsened；rank 低（更正）→ improved；同级 → 看强度。"""
        emotions = list(EMOTION_RANK)
        for a in emotions:
            for b in emotions:
                ra, rb = EMOTION_RANK[a], EMOTION_RANK[b]
                chg = emotion_change(item(1, a), item(2, b))
                if ra > rb:
                    assert chg == "improved", f"{a}({ra}) → {b}({rb})"
                elif ra < rb:
                    assert chg == "worsened", f"{a}({ra}) → {b}({rb})"
                else:
                    assert chg == "unchanged", f"{a}={b} 同级强度相同"

    def test_same_rank_intensity_decrease_is_improved(self):
        """同级情绪强度下降 → improved（如 担忧3 → 担忧1）。"""
        assert emotion_change(item(1, "担忧", intensity=3), item(2, "担忧", intensity=1)) == "improved"
        assert emotion_change(item(1, "愤怒", intensity=5), item(2, "愤怒", intensity=2)) == "improved"

    def test_same_rank_intensity_increase_is_worsened(self):
        assert emotion_change(item(1, "担忧", intensity=1), item(2, "担忧", intensity=3)) == "worsened"

    def test_rank_wins_over_intensity(self):
        """负面程度优先于强度：愤怒1 → 担忧5 仍是 improved（更正面）。"""
        assert emotion_change(item(1, "愤怒", intensity=1), item(2, "担忧", intensity=5)) == "improved"
        assert emotion_change(item(1, "积极/认可", intensity=5), item(2, "中性", intensity=0)) == "worsened"

    def test_negative_set_contains_six(self):
        assert NEGATIVE_EMOTIONS == {"担忧", "怀疑", "失望", "焦虑", "不满", "愤怒"}


# ---------- build_summary：时间线 / 归属 / 每助理统计 ----------

def messages_from(turns):
    """turns: [(role, turn_no, assistant_id 或 None), ...] 顺序即时间序。"""
    out = []
    for role, no, aid in turns:
        m = mk(role, no, canonical_name=("王萌" if aid == 1 else "李金潓") if role == "助" else None)
        if role == "助":
            m.assistant_id = aid
        out.append(m)
    return out


class TestBuildSummaryTimeline:
    def test_single_message_no_pairs(self):
        """单条客户消息：时间线基准（change=None），无任何变化统计。"""
        msgs = [mk("客", 1, "你好")]
        s = build_summary(msgs, [item(1, "中性")])
        assert s["changes"] == {"total": 0, "improved": 0, "worsened": 0, "unchanged": 0, "not_judged": 0}
        assert len(s["timeline"]) == 1
        assert s["timeline"][0]["change"] is None
        assert s["current"]["turn_no"] == 1
        assert s["per_assistant"] == []

    def test_multi_assistant_user_scenario(self):
        """用户示例会话：客1(焦虑) → 助A → 客2(担忧) → 助A → 客3(积极) → 助B → 客4(中性)。
        归属到最后回复的助理；统计与时间线方向分离。"""
        msgs = [
            mk("客", 1, "有点慌"),
            mk("助", 2, "别慌", assistant_id=1, canonical_name="王萌"),
            mk("客", 3, "还是担心"),
            mk("助", 4, "我帮您看着", assistant_id=1, canonical_name="王萌"),
            mk("客", 5, "好的谢谢"),
            mk("助", 6, "不客气", assistant_id=2, canonical_name="李金潓"),
            mk("客", 7, "那我再等等"),
        ]
        items = [item(1, "焦虑", 4), item(3, "担忧", 3), item(5, "积极/认可", 2), item(7, "中性", 0)]
        s = build_summary(msgs, items)
        # 时间线：焦虑→担忧 improved；担忧→积极 improved；积极→中性 worsened
        assert [t["change"] for t in s["timeline"]] == [None, "improved", "improved", "worsened"]
        assert s["changes"] == {"total": 3, "improved": 2, "worsened": 1, "unchanged": 0, "not_judged": 0}
        assert (s["current"]["turn_no"], s["current"]["emotion"]) == (7, "中性")
        assert s["current"]["change"] == "worsened"  # current 即时间线末条（含 change）
        by = {p["assistant_name"]: p for p in s["per_assistant"]}
        # 王萌：客2（担忧）负面；pair(1,3) 与 pair(3,5) 都归王萌（最后一条回复是她）
        wm = by["王萌"]
        assert wm["negative_count"] == 1
        assert (wm["improved"], wm["worsened"], wm["unchanged"], wm["evaluable_pairs"]) == (2, 0, 0, 2)
        assert wm["improve_rate"] == 1.0
        # 李金潓：客4 中性不负面；pair(5,7) 归她 → worsened
        ljy = by["李金潓"]
        assert ljy["negative_count"] == 0
        assert (ljy["improved"], ljy["worsened"], ljy["unchanged"], ljy["evaluable_pairs"]) == (0, 1, 0, 1)
        assert ljy["improve_rate"] == 0.0
        assert s["negative_count"] == 1

    def test_attribution_to_last_reply_between_pair(self):
        """客户消息对之间多条助轮 → 归属最后一条（离下一条客轮最近的回复）。"""
        msgs = [
            mk("客", 1),
            mk("助", 2, assistant_id=1, canonical_name="王萌"),
            mk("助", 3, assistant_id=2, canonical_name="李金潓"),
            mk("客", 4),
        ]
        s = build_summary(msgs, [item(1, "焦虑"), item(4, "中性")])
        by = {p["assistant_name"]: p for p in s["per_assistant"]}
        assert "王萌" not in by
        assert by["李金潓"]["improved"] == 1
        assert by["李金潓"]["evaluable_pairs"] == 1
        # negative_count 归属也是最近助轮（客1 前无 → 不计；客4 中性不负面）
        assert by["李金潓"]["negative_count"] == 0

    def test_consecutive_customer_messages_not_judged(self):
        """连续两条客户消息中间无助理回复 → 计入时间线总变化但 not_judged，不进任何助理。"""
        msgs = [
            mk("客", 1),
            mk("客", 2),
            mk("助", 3, assistant_id=1, canonical_name="王萌"),
            mk("客", 4),
        ]
        s = build_summary(msgs, [item(1, "担忧"), item(2, "焦虑"), item(4, "中性")])
        assert s["changes"] == {"total": 2, "improved": 1, "worsened": 1, "unchanged": 0, "not_judged": 1}
        by = {p["assistant_name"]: p for p in s["per_assistant"]}
        # pair(1,2) 无助轮 → not_judged；pair(2,4) 归王萌
        assert by["王萌"]["evaluable_pairs"] == 1
        assert by["王萌"]["improved"] == 1

    def test_first_message_negative_not_counted(self):
        """首条客户消息为负面但其前无助理回复 → 不计入任何助理负面次数。"""
        msgs = [mk("客", 1), mk("助", 2, assistant_id=1, canonical_name="王萌"), mk("客", 3)]
        s = build_summary(msgs, [item(1, "焦虑"), item(3, "中性")])
        by = {p["assistant_name"]: p for p in s["per_assistant"]}
        assert by["王萌"]["negative_count"] == 0

    def test_improve_rate_zero_when_pairs_no_improvement(self):
        """助理有可评估对但零改善 → improve_rate=0.0（分母非零）。"""
        msgs = [mk("助", 1, assistant_id=1, canonical_name="王萌"), mk("客", 2), mk("助", 3, assistant_id=1, canonical_name="王萌"), mk("客", 4)]
        s = build_summary(msgs, [item(2, "担忧"), item(4, "焦虑")])
        by = {p["assistant_name"]: p for p in s["per_assistant"]}
        wm = by["王萌"]
        assert wm["negative_count"] == 2
        assert wm["evaluable_pairs"] == 1
        assert wm["improved"] == 0 and wm["worsened"] == 1
        assert wm["improve_rate"] == 0.0

    def test_improve_rate_none_zero_denominator(self):
        """无任何 pair 的助理（负面客轮后会话结束）→ improve_rate=None，前端显示 —。"""
        msgs = [mk("助", 1, assistant_id=1, canonical_name="王萌"), mk("客", 2)]
        s = build_summary(msgs, [item(2, "焦虑")])
        by = {p["assistant_name"]: p for p in s["per_assistant"]}
        assert by["王萌"]["negative_count"] == 1
        assert by["王萌"]["evaluable_pairs"] == 0
        assert by["王萌"]["improve_rate"] is None

    def test_timeline_change_ignores_assistant_presence(self):
        """时间线变化恒算全部相邻客轮对（无论中间是否有助理回复）；not_judged 只影响助理归属。"""
        msgs = [mk("客", 1), mk("客", 2), mk("客", 3)]
        s = build_summary(msgs, [item(1, "担忧"), item(2, "中性"), item(3, "中性")])
        assert s["changes"] == {"total": 2, "improved": 1, "worsened": 0, "unchanged": 1, "not_judged": 2}
        assert s["per_assistant"] == []

    def test_mixed_unchanged_same_emotion(self):
        """同情绪同强度 → unchanged 计入该助理。"""
        msgs = [mk("客", 1), mk("助", 2, assistant_id=1, canonical_name="王萌"), mk("客", 3)]
        s = build_summary(msgs, [item(1, "中性"), item(3, "中性")])
        by = {p["assistant_name"]: p for p in s["per_assistant"]}
        assert by["王萌"]["unchanged"] == 1
        assert by["王萌"]["improve_rate"] == 0.0


class TestBuildSummarySummary:
    def test_main_triggers_excludes_unknown_other(self):
        """触发原因统计：排除 未知/其他，按次数降序 top3。"""
        msgs = [mk("客", i) for i in (1, 2, 3, 4, 5)]
        items = [
            item(1, "担忧", trigger="持仓亏损"),
            item(2, "担忧", trigger="持仓亏损"),
            item(3, "焦虑", trigger="行情波动"),
            item(4, "中性", trigger="未知"),
            item(5, "中性", trigger="其他"),
        ]
        s = build_summary(msgs, items)
        assert s["main_triggers"] == [
            {"trigger": "持仓亏损", "count": 2},
            {"trigger": "行情波动", "count": 1},
        ]

    def test_main_triggers_all_excluded_empty(self):
        msgs = [mk("客", 1), mk("客", 2)]
        s = build_summary(msgs, [item(1, "中性", trigger="未知"), item(2, "中性", trigger="未知")])
        assert s["main_triggers"] == []

    def test_low_confidence_count(self):
        """低置信度 = synthesized 或 confidence < 阈值。"""
        msgs = [mk("客", 1), mk("客", 2), mk("客", 3), mk("客", 4)]
        items = [
            item(1, "中性", confidence=0.9),
            item(2, "担忧", confidence=0.3),
            item(3, "中性", confidence=0.9, synthesized=True),
            item(4, "中性", confidence=LOW_CONFIDENCE_THRESHOLD),  # 恰好等于阈值 → 不算低
        ]
        s = build_summary(msgs, items)
        assert s["low_confidence_count"] == 2

    def test_current_is_last_customer_item(self):
        msgs = [mk("客", 1), mk("客", 5)]
        s = build_summary(msgs, [item(1, "担忧"), item(5, "积极/认可")])
        assert s["current"]["turn_no"] == 5
        assert s["current"]["emotion"] == "积极/认可"

    def test_per_assistant_sorted_by_name(self):
        msgs = [
            mk("客", 1),
            mk("助", 2, assistant_id=2, canonical_name="李金潓"),
            mk("客", 3),
            mk("助", 4, assistant_id=1, canonical_name="王萌"),
            mk("客", 5),
        ]
        s = build_summary(msgs, [item(1, "担忧"), item(3, "中性"), item(5, "中性")])
        names = [p["assistant_name"] for p in s["per_assistant"]]
        assert names == ["李金潓", "王萌"]
