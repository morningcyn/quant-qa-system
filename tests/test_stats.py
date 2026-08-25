import json
from datetime import datetime, timedelta

from backend.config import DEFAULT_TEMPLATES
from backend.db import repository
from backend.services import report


def _save_inspection(session, assistant_id, score, created_at, d_scores=None, s_scores=None):
    snapshot = DEFAULT_TEMPLATES["standard"]
    if d_scores is None:
        d_scores = {"d1_emotion_change": {"score": 8}}
    if s_scores is None:
        s_scores = {"s1_emotion_stabilize": {"score": 15, "sub_items": {"empathy": 4, "customized": 4, "direct": 3, "no_conflict": 4, "vent_guide": 0}}}
    inspection = repository.save_inspection(
        session,
        assistant_id=assistant_id,
        session_title=None,
        total_score=score,
        is_yellow_alert=score < 59,
        yellow_alert_reasons=[],
        template_type="standard",
        template_snapshot=snapshot,
        turn_count=8,
        customer_profile="焦虑型",
        raw_dialogue="[客] 你好\n[助] 您好",
        d_scores=d_scores,
        s_scores=s_scores,
        highlight_dialogue=[],
        suggestions=[],
    )
    inspection.created_at = created_at
    session.commit()
    return inspection


class TestTrend:
    def test_zero_fill_and_avg(self, session):
        assistant = repository.create_assistant(session, "张三", "E001", "standard")
        now = datetime.now()
        _save_inspection(session, assistant.id, 80, now - timedelta(seconds=2))
        _save_inspection(session, assistant.id, 60, now)
        _save_inspection(session, assistant.id, 40, now - timedelta(days=10))
        result = report.trend_stats(session, assistant.id, days=30)
        assert len(result["points"]) == 30
        today = result["points"][-1]
        assert today["count"] == 2
        assert today["avg_score"] == 70.0
        null_days = [p for p in result["points"] if p["avg_score"] is None]
        assert len(null_days) == 28  # 30 天里只有 2 天有数据（今天 2 条 + 10 天前 1 条）
        assert result["total_count"] == 3
        assert result["yellow_count"] == 1
        assert result["latest_score"] == 60


class TestTop3:
    def test_dimension_and_subitem_aggregation(self, session):
        assistant = repository.create_assistant(session, "张三", "E001", "standard")
        now = datetime.now()
        # 三次质检：S1 得 15/20（失分5）、D1 得 8/10（失分2）、empathy 得 2/4（失分2）
        for score in (70, 60, 50):
            _save_inspection(
                session,
                assistant.id,
                score,
                now,
                d_scores={
                    "d1_emotion_change": {"score": 8},
                    "d2_profile_match": {"score": 14},
                    "d3_problem_match": {"score": 14},
                    "d4_expectation_exceed": {"score": 14},
                },
                s_scores={
                    "s1_emotion_stabilize": {"score": 15, "sub_items": {"empathy": 2, "customized": 4, "direct": 3, "no_conflict": 4, "vent_guide": 2}},
                    "s2_problem_closure": {"score": 14, "sub_items": {"completeness": 4, "structure": 4, "next_step": 2, "follow_up": 4}},
                    "s3_professional_supply": {"score": 9, "sub_items": {"logic": 4, "explain_why": 2, "decision_ownership": 3}},
                },
            )
        result = report.top3_loss(session, assistant.id, days=30)
        dims = result["dimensions"]
        assert dims[0]["key"] == "s1"  # S1 每次失分 5，累计 15 最高
        assert dims[0]["loss_total"] == 15
        assert dims[0]["occurrence_count"] == 3
        subs = result["sub_items"]
        top = subs[0]
        assert top["key"] == "s1.empathy"  # empathy 每次失分 2，累计 6 最高
        assert top["loss_total"] == 6
        assert top["avg_score"] == 2.0
