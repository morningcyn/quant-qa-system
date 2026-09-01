# 客户情绪曲线：EMOTION_SCORE 全表 / build_curve 派生规则（转折/风险/助理节点/统计/降级）
# analyzer 落库扩展（score/快照/curve）/ API 透传与旧行降级派生
# 曲线全部为确定性派生，零 LLM 调用。
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.db import repository
from backend.db.database import get_db
from backend.db.models import EmotionSession
from backend.main import app
from backend.services.emotion.analyzer import analyze_session
from backend.services.emotion.derive import EMOTION_SCORE, build_curve
from backend.services.multiparser import MultiMessage

from tests.conftest import MockLLMClient
from tests.test_emotion_api import dialogue_llm_json, make_multi_report


@pytest.fixture()
def client(session, monkeypatch):
    """TestClient + 内存库；情绪 LLM 用 mock；隔离 name_map 与真实库。"""
    monkeypatch.setattr("backend.main.mgr.resume_all", lambda: None)
    monkeypatch.setattr("backend.api.batch.multiparser.load_name_map", lambda: {})
    monkeypatch.setattr("backend.services.emotion.analyzer.multiparser.load_name_map", lambda: {})
    monkeypatch.setattr("backend.services.emotion.analyzer.multiparser.load_not_assistant_names", lambda: [])

    llm = MockLLMClient([dialogue_llm_json()])
    monkeypatch.setattr(
        "backend.services.llm.factory.get_active_runtime",
        lambda s: (llm, {"temperature": 0.1}),
    )

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        yield c, session, llm
    app.dependency_overrides.clear()


def row(turn_no, role, text="x", assistant_id=None, canonical_name=None, timestamp=None):
    """新格式快照行（analyzer 落库 messages_json 的结构）。"""
    name = "客户" if role == "客" else (canonical_name or f"助{turn_no}")
    return {
        "turn_no": turn_no, "role": role, "speaker": name,
        "canonical_name": name, "text": text, "timestamp": timestamp,
        "assistant_id": assistant_id,
    }


def item(turn_no, emotion, intensity=3, confidence=0.9, trigger="行情波动", **over):
    d = {
        "turn_no": turn_no, "emotion": emotion, "intensity": intensity,
        "confidence": confidence, "trigger": trigger, "evidence": "原话",
        "synthesized": False, "evidence_adjusted": False,
    }
    d.update(over)
    return d


# ---------- EMOTION_SCORE：用户给定映射全表 ----------

class TestEmotionScore:
    def test_full_mapping_table(self):
        """8 类情绪 → 分值精确匹配用户映射。"""
        assert EMOTION_SCORE == {
            "积极/认可": 2, "中性": 0, "担忧": -1, "怀疑": -1,
            "失望": -2, "焦虑": -2, "不满": -3, "愤怒": -4,
        }

    def test_score_is_deterministic_for_all_emotions(self):
        for emotion, score in EMOTION_SCORE.items():
            assert score in (-4, -3, -2, -1, 0, 2)  # 只用粗粒度刻度，无虚假精确度


# ---------- build_curve：助理回复节点（第六节） ----------

class TestBuildCurveAssistantReplies:
    def test_change_four_states(self):
        """助理回复前后情绪：improved / worsened / unchanged / unknown（头尾无客户消息）。"""
        # 会话：客1(焦虑) 助2 客3(中性) → improved；助4(结尾，after=None) → unknown
        rows = [
            row(1, "客", "有点慌", timestamp="2026-08-01 10:00:00"),
            row(2, "助", "别急", assistant_id=1, canonical_name="王萌", timestamp="2026-08-01 10:01:00"),
            row(3, "客", "好的", timestamp="2026-08-01 10:02:00"),
            row(4, "助", "再见", assistant_id=1, canonical_name="王萌", timestamp="2026-08-01 10:03:00"),
        ]
        items = [item(1, "焦虑"), item(3, "中性")]
        c = build_curve(rows, items)
        a1, a2 = c["assistant_replies"]
        assert (a1["before"]["turn_no"], a1["after"]["turn_no"], a1["change"]) == (1, 3, "improved")
        assert a1["assistant_name"] == "王萌" and a1["assistant_id"] == 1
        assert a1["timestamp"] == "2026-08-01 10:01:00" and a1["text"] == "别急"
        assert a2["before"]["turn_no"] == 3 and a2["after"] is None and a2["change"] == "unknown"

    def test_reply_at_session_start_unknown(self):
        """回复在会话开头（before=None）→ unknown。"""
        rows = [
            row(1, "助", "您好", assistant_id=1, canonical_name="王萌", timestamp="10:00"),
            row(2, "客", "你好", timestamp="10:01"),
        ]
        c = build_curve(rows, [item(2, "中性")])
        assert c["assistant_replies"][0]["before"] is None
        assert c["assistant_replies"][0]["change"] == "unknown"

    def test_two_replies_between_same_pair_each_evaluated(self):
        """两条助轮夹在同一对客户消息间 → 各自独立评估（不归属、不修改 per_assistant）。"""
        rows = [
            row(1, "客", "有点慌", timestamp="10:00"),
            row(2, "助", "别急", assistant_id=1, canonical_name="王萌", timestamp="10:01"),
            row(3, "助", "我也看看", assistant_id=2, canonical_name="李金潓", timestamp="10:02"),
            row(4, "客", "好的", timestamp="10:03"),
        ]
        c = build_curve(rows, [item(1, "焦虑"), item(4, "中性")])
        reps = c["assistant_replies"]
        assert len(reps) == 2
        assert [r["change"] for r in reps] == ["improved", "improved"]
        assert [r["assistant_name"] for r in reps] == ["王萌", "李金潓"]
        assert [r["before"]["turn_no"] for r in reps] == [1, 1]
        assert [r["after"]["turn_no"] for r in reps] == [4, 4]

    def test_worsened_and_unchanged(self):
        rows = [
            row(1, "客", "好的", timestamp="10:00"),
            row(2, "助", "您好", assistant_id=1, canonical_name="王萌", timestamp="10:01"),
            row(3, "客", "有点担心", timestamp="10:02"),
            row(4, "助", "没事的", assistant_id=1, canonical_name="王萌", timestamp="10:03"),
            row(5, "客", "嗯嗯", timestamp="10:04"),
        ]
        items = [item(1, "中性"), item(3, "担忧"), item(5, "中性", intensity=0)]
        c = build_curve(rows, items)
        chgs = [r["change"] for r in c["assistant_replies"]]
        assert chgs == ["worsened", "improved"]  # 中性→担忧；担忧→中性

    def test_timestamp_order_keeps_reply_bound_to_chronological_customer_emotions(self):
        """时间戳与 turn_no 不一致时，曲线和助理前后情绪均按真实时间排列。"""
        rows = [
            row(1, "客", "先说", timestamp="2026-08-01 10:00:00"),
            row(2, "客", "后说", timestamp="2026-08-01 10:03:00"),
            row(3, "助", "针对先说回复", assistant_id=1, canonical_name="王萌", timestamp="2026-08-01 10:01:00"),
            row(4, "助", "针对后说回复", assistant_id=1, canonical_name="王萌", timestamp="2026-08-01 10:04:00"),
        ]
        c = build_curve(rows, [item(1, "焦虑"), item(2, "中性")])

        assert [p["turn_no"] for p in c["points"]] == [1, 2]
        assert [p["timestamp"] for p in c["points"]] == [
            "2026-08-01 10:00:00", "2026-08-01 10:03:00"
        ]
        first, second = c["assistant_replies"]
        assert (first["turn_no"], first["before"]["turn_no"], first["after"]["turn_no"]) == (3, 1, 2)
        assert first["change"] == "improved"
        assert (second["turn_no"], second["before"]["turn_no"], second["after"]) == (4, 2, None)
        assert [p["sequence"] for p in c["points"]] == [0, 2]
        assert [a["sequence"] for a in c["assistant_replies"]] == [1, 3]


# ---------- build_curve：转折点（第七节） ----------

class TestBuildCurveTurningPoints:
    def _points(self, emotions):
        """构造连续客轮 + 全量情绪，返回 build_curve 结果。"""
        rows = [row(i + 1, "客", f"消息{i + 1}", timestamp=f"10:0{i}") for i in range(len(emotions))]
        items = [item(i + 1, e, intensity=3) for i, e in enumerate(emotions)]
        return build_curve(rows, items)

    def test_user_examples_turning_points(self):
        """用户第七节示例：Δ≥2 全部标为转折点。"""
        cases = [
            (["中性", "焦虑"], [(2, "中性", "焦虑", 0, -2)]),
            (["担忧", "不满"], [(2, "担忧", "不满", -1, -3)]),
            (["不满", "中性"], [(2, "不满", "中性", -3, 0)]),
            (["中性", "积极/认可"], [(2, "中性", "积极/认可", 0, 2)]),
        ]
        for emotions, expected in cases:
            c = self._points(emotions)
            tps = c["turning_points"]
            assert len(tps) == len(expected)
            tp = tps[0]
            e = expected[0]
            assert (tp["turn_no"], tp["prev_emotion"], tp["next_emotion"], tp["prev_score"], tp["next_score"]) == e
            assert tp["evidence"] == "原话"
            assert tp["timestamp"] == "10:01"
            assert tp["change"] in ("improved", "worsened")

    def test_delta_below_two_not_marked(self):
        """边界：担忧(-1)→中性(0) 差 1 → 不标转折点。"""
        c = self._points(["担忧", "中性"])
        assert c["turning_points"] == []
        assert c["stats"]["turning_count"] == 0

    def test_same_score_different_emotion_not_marked(self):
        """同分值（担忧/怀疑均 -1）→ 不标。"""
        c = self._points(["担忧", "怀疑"])
        assert c["turning_points"] == []

    def test_only_delta_two_plus(self):
        c = self._points(["中性", "中性", "担忧", "不满", "中性", "积极/认可"])
        turns = [tp["turn_no"] for tp in c["turning_points"]]
        assert turns == [4, 5, 6]  # 中性→担忧(1)不标；担忧→不满(2)标；不满→中性(3)标；中性→积极(2)标


# ---------- build_curve：风险点 / 统计 / 降级 ----------

class TestBuildCurveRiskPoint:
    def test_most_negative_wins(self):
        rows = [row(i + 1, "客", f"消息{i + 1}", timestamp=f"10:0{i}") for i in range(5)]
        items = [item(1, "担忧", 2), item(2, "中性"), item(3, "焦虑", 4), item(4, "不满", 5), item(5, "积极/认可")]
        c = build_curve(rows, items)
        rp = c["risk_point"]
        assert (rp["turn_no"], rp["emotion"], rp["emotion_score"]) == (4, "不满", -3)
        assert rp["emotion_intensity"] == 5 and rp["evidence"] == "原话"

    def test_tie_breaks_by_intensity_then_earliest(self):
        """并列最低分 → 取强度大者；强度也并列 → 取先出现。"""
        rows = [row(i + 1, "客", f"消息{i + 1}", timestamp=f"10:0{i}") for i in range(3)]
        items = [item(1, "失望", 2), item(2, "焦虑", 4), item(3, "焦虑", 4)]
        c = build_curve(rows, items)  # 失望/焦虑 同分 -2；焦虑强度 4 > 2 → turn2
        assert c["risk_point"]["turn_no"] == 2
        items2 = [item(1, "焦虑", 3), item(2, "失望", 3)]  # 同分同强度 → 先出现
        c2 = build_curve(rows, items2)
        assert c2["risk_point"]["turn_no"] == 1

    def test_no_negative_null(self):
        rows = [row(1, "客", "好的", timestamp="10:00"), row(2, "客", "谢谢", timestamp="10:01")]
        c = build_curve(rows, [item(1, "中性"), item(2, "积极/认可")])
        assert c["risk_point"] is None


class TestBuildCurveStats:
    def test_initial_lowest_final_and_counts(self):
        rows = [
            row(1, "客", "有点慌", timestamp="10:00"),
            row(2, "助", "别急", assistant_id=1, canonical_name="王萌", timestamp="10:01"),
            row(3, "客", "还是担心", timestamp="10:02"),
            row(4, "客", "好的谢谢", timestamp="10:03"),
        ]
        items = [item(1, "焦虑", 4), item(3, "担忧", 3), item(4, "积极/认可", 2)]
        c = build_curve(rows, items)
        st = c["stats"]
        assert st["initial"] == {"turn_no": 1, "emotion": "焦虑", "emotion_score": -2}
        assert st["lowest"] == {"turn_no": 1, "emotion": "焦虑", "emotion_score": -2}  # -2 < -1
        assert st["final"] == {"turn_no": 4, "emotion": "积极/认可", "emotion_score": 2}
        # 焦虑(-2)→担忧(-1) improved（Δ1 不算转折）；担忧(-1)→积极(+2) improved（Δ3 算转折）
        assert (st["improved_count"], st["worsened_count"], st["turning_count"]) == (2, 0, 1)

    def test_worsened_count_and_lowest_positive_ties(self):
        rows = [row(i + 1, "客", f"消息{i + 1}", timestamp=f"10:0{i}") for i in range(3)]
        items = [item(1, "积极/认可", 2), item(2, "中性"), item(3, "担忧", 2)]
        c = build_curve(rows, items)
        st = c["stats"]
        assert st["initial"]["emotion"] == "积极/认可" and st["final"]["emotion"] == "担忧"
        assert st["lowest"] == {"turn_no": 3, "emotion": "担忧", "emotion_score": -1}  # 含非负面取最低
        assert (st["improved_count"], st["worsened_count"]) == (0, 2)  # 积极→中性、中性→担忧

    def test_single_message(self):
        rows = [row(1, "客", "你好", timestamp="10:00")]
        c = build_curve(rows, [item(1, "中性")])
        st = c["stats"]
        assert st["initial"] == st["lowest"] == st["final"]
        assert st["improved_count"] == st["worsened_count"] == st["turning_count"] == 0

    def test_points_sequence_with_timestamps(self):
        rows = [
            row(1, "客", "a", timestamp="10:00"),
            row(2, "助", "b", assistant_id=1, canonical_name="王萌", timestamp="10:01"),
            row(3, "客", "c", timestamp="10:02"),
        ]
        c = build_curve(rows, [item(1, "担忧"), item(3, "中性")])
        pts = c["points"]
        assert [p["turn_no"] for p in pts] == [1, 3]
        assert [p["timestamp"] for p in pts] == ["10:00", "10:02"]
        assert pts[0]["emotion"] == "担忧" and pts[0]["emotion_score"] == -1
        assert pts[0]["emotion_intensity"] == 3

    def test_items_without_score_are_backfilled(self):
        """旧行 items 无 emotion_score → build_curve 按情绪补算。"""
        rows = [row(1, "客", "有点慌", timestamp="10:00"), row(2, "客", "好的", timestamp="10:01")]
        items = [item(1, "焦虑"), item(2, "中性")]
        items[0].pop("emotion_score", None)  # item() helper 无 score 键，显式确认
        assert "emotion_score" not in items[0]
        c = build_curve(rows, items)
        assert c["points"][0]["emotion_score"] == -2  # 焦虑
        assert c["turning_points"][0]["prev_score"] == -2  # 焦虑→中性 Δ2
        assert c["stats"]["lowest"]["emotion_score"] == -2


class TestBuildCurveDegraded:
    def test_old_format_rows_degraded(self):
        """旧快照（无 role/timestamp，仅客户消息）→ 降级：无助理节点、无时间、degraded=true。"""
        old_rows = [
            {"turn_no": 1, "speaker": "客户", "text": "有点慌"},
            {"turn_no": 2, "speaker": "客户", "text": "好的"},
        ]
        c = build_curve(old_rows, [item(1, "焦虑"), item(2, "中性")])
        assert c["degraded"] is True
        assert c["assistant_replies"] == []
        assert [p["timestamp"] for p in c["points"]] == [None, None]
        # 但曲线仍可画：客轮点齐全、转折点仍派生
        assert c["points"][0]["emotion_score"] == -2
        assert c["turning_points"][0]["change"] == "improved"

    def test_old_format_with_masked_customer_name(self):
        """旧快照客户 speaker 为脱敏名（非"客户"）→ 仍按客轮处理（旧快照只存客户消息）。"""
        old_rows = [
            {"turn_no": 1, "speaker": "张*", "text": "有点慌"},
            {"turn_no": 3, "speaker": "张*", "text": "好的"},
        ]
        c = build_curve(old_rows, [item(1, "焦虑"), item(3, "中性")])
        assert c["degraded"] is True
        assert [p["turn_no"] for p in c["points"]] == [1, 3]  # 不再被误判为助轮
        assert c["turning_points"][0]["change"] == "improved"

    def test_full_rows_not_degraded(self):
        rows = [
            row(1, "客", "你好", timestamp="10:00"),
            row(2, "助", "您好", assistant_id=1, canonical_name="王萌", timestamp="10:01"),
        ]
        c = build_curve(rows, [item(1, "中性")])
        assert c["degraded"] is False
        assert len(c["assistant_replies"]) == 1

    def test_missing_timestamp_but_has_assistant_degraded(self):
        """有助轮但全部无时间戳 → 仍降级（时间轴缺失）。"""
        rows = [
            row(1, "客", "你好"),
            row(2, "助", "您好", assistant_id=1, canonical_name="王萌"),
        ]
        c = build_curve(rows, [item(1, "中性")])
        assert c["degraded"] is True
        assert len(c["assistant_replies"]) == 1
        assert c["assistant_replies"][0]["timestamp"] is None


# ---------- analyzer 落库扩展 ----------

def mk(role, turn_no, text="好的", assistant_id=None, canonical_name=None, timestamp=None):
    if role == "客":
        speaker, canonical = "客户", "客户"
    else:
        canonical = canonical_name or f"助{turn_no}"
        speaker = canonical
    return MultiMessage(
        turn_no=turn_no, role=role, speaker=speaker, canonical_name=canonical,
        text=text, timestamp=timestamp, assistant_id=assistant_id, raw_line="",
    )


class TestAnalyzeSessionCurve:
    def test_items_have_score_and_snapshot_full(self, session):
        """落库：items 每条含 emotion_score；messages_json 全量含助轮与时间戳；summary.curve 一并落库。"""
        msgs = [
            mk("客", 1, "有点慌", timestamp="2026-08-01 10:00:00"),
            mk("助", 2, "别急", 1, "王萌", "2026-08-01 10:01:00"),
            mk("客", 3, "好的", timestamp="2026-08-01 10:02:00"),
        ]
        client = MockLLMClient([dialogue_llm_json()])
        import asyncio

        asyncio.run(
            analyze_session(
                session, msgs=msgs, title="曲线会话", conversation_id="curve-1",
                source_type="multi", customer_name=None, client=client, cfg={},
            )
        )
        row = repository.get_emotion_session_by_conversation(session, "curve-1")
        items = json.loads(row.items_json)
        assert {i["turn_no"]: i["emotion_score"] for i in items} == {1: -2, 3: 0}  # 焦虑/中性
        snap = json.loads(row.messages_json)
        assert [m["turn_no"] for m in snap] == [1, 2, 3]
        asst = snap[1]
        assert asst["role"] == "助" and asst["assistant_id"] == 1
        assert asst["canonical_name"] == "王萌" and asst["timestamp"] == "2026-08-01 10:01:00"
        assert snap[0]["role"] == "客" and snap[0]["timestamp"] == "2026-08-01 10:00:00"
        summary = json.loads(row.summary_json)
        curve = summary["curve"]
        assert curve["degraded"] is False
        assert len(curve["assistant_replies"]) == 1
        assert curve["assistant_replies"][0]["change"] == "improved"  # 焦虑→中性
        assert curve["assistant_replies"][0]["timestamp"] == "2026-08-01 10:01:00"
        assert [p["timestamp"] for p in curve["points"]] == ["2026-08-01 10:00:00", "2026-08-01 10:02:00"]
        assert curve["risk_point"]["emotion"] == "焦虑"
        assert curve["stats"]["final"]["emotion"] == "中性"
        # timeline 透传 emotion_score（前端曲线数据自包含）
        assert [t["emotion_score"] for t in summary["timeline"]] == [-2, 0]

    def test_synthesized_neutral_score_zero(self, session):
        """合成兜底中性 → emotion_score=0。"""
        msgs = [mk("客", 1, ""), mk("客", 2, "你好")]
        import asyncio

        client = MockLLMClient(
            [
                json.dumps(
                    {"items": [{"turn_no": 2, "emotion": "中性", "intensity": 0, "confidence": 0.9, "trigger": "未知", "evidence": "你好"}]},
                    ensure_ascii=False,
                )
            ]
        )
        asyncio.run(
            analyze_session(
                session, msgs=msgs, title="t", conversation_id="curve-syn",
                source_type="multi", customer_name=None, client=client, cfg={},
            )
        )
        row = repository.get_emotion_session_by_conversation(session, "curve-syn")
        items = {i["turn_no"]: i for i in json.loads(row.items_json)}
        assert items[1]["emotion_score"] == 0 and items[2]["emotion_score"] == 0


# ---------- API：curve 透传 / 旧行降级派生 ----------

class TestEmotionApiCurve:
    def test_new_row_curve_passthrough(self, client):
        """新生成的行：GET 直接返回落库的 curve（含助理节点与时间戳）。"""
        c, session, _ = client
        ins = make_multi_report(session, conversation_id="conv-curve-new")
        resp = c.post("/api/emotion/analyze", json={"inspection_id": ins.id})
        assert resp.status_code == 200, resp.text
        curve = resp.json()["curve"]
        assert curve["degraded"] is False
        assert len(curve["assistant_replies"]) == 1
        assert curve["assistant_replies"][0]["assistant_name"] == "王萌"
        assert curve["assistant_replies"][0]["timestamp"] == "2026-08-01 10:01:00"
        assert curve["assistant_replies"][0]["change"] == "improved"
        assert curve["stats"]["turning_count"] == 1  # 焦虑(-2)→中性(0) Δ2
        assert curve["stats"]["initial"]["emotion"] == "焦虑"
        assert curve["stats"]["final"]["emotion"] == "中性"
        # GET 与 POST 一致
        d2 = c.get(f"/api/emotion/inspection/{ins.id}").json()
        assert d2["curve"] == curve

    def test_old_row_degraded_derived(self, client):
        """曲线上线前生成的旧行（无 curve、items 无 score、快照仅客户消息）：
        GET 实时派生 curve（degraded=true、无助理节点）并给 timeline 补 score。"""
        c, session, _ = client
        ins = make_multi_report(session, conversation_id="conv-curve-old")
        repository.save_emotion_session(
            session, "conv-curve-old", "multi", "张三会话", "张三",
            items=[  # 旧格式：无 emotion_score
                {"turn_no": 1, "emotion": "焦虑", "intensity": 3, "confidence": 0.9, "trigger": "持仓亏损", "evidence": "有点慌", "synthesized": False, "evidence_adjusted": False},
                {"turn_no": 3, "emotion": "中性", "intensity": 0, "confidence": 0.9, "trigger": "未知", "evidence": "好的", "synthesized": False, "evidence_adjusted": False},
            ],
            summary={
                "current": {"turn_no": 3, "emotion": "中性"},
                "timeline": [
                    {"turn_no": 1, "emotion": "焦虑", "intensity": 3, "change": None},
                    {"turn_no": 3, "emotion": "中性", "intensity": 0, "change": "improved"},
                ],
                "changes": {"total": 1, "improved": 1, "worsened": 0, "unchanged": 0, "not_judged": 0},
                "negative_count": 1, "low_confidence_count": 0, "main_triggers": [],
                "per_assistant": [],
            },
            messages=[  # 旧快照：仅客户消息、无 role/timestamp
                {"turn_no": 1, "speaker": "客户", "text": "有点慌"},
                {"turn_no": 3, "speaker": "客户", "text": "好的"},
            ],
            degraded=False,
        )
        d = c.get(f"/api/emotion/inspection/{ins.id}").json()
        curve = d["curve"]
        assert curve["degraded"] is True
        assert curve["assistant_replies"] == []
        assert [p["timestamp"] for p in curve["points"]] == [None, None]
        assert curve["risk_point"]["emotion"] == "焦虑" and curve["risk_point"]["emotion_score"] == -2
        assert curve["turning_points"][0]["change"] == "improved"
        # 旧行 timeline 响应中补上 emotion_score（不影响落库）
        assert [t["emotion_score"] for t in d["timeline"]] == [-2, 0]
        row = repository.get_emotion_session_by_conversation(session, "conv-curve-old")
        stored = json.loads(row.summary_json)
        assert "emotion_score" not in stored["timeline"][0]  # 落库未被修改

    def test_no_curve_but_corrupt_json_safe(self, client):
        """快照/items JSON 异常 → 不崩，曲线为空骨架。"""
        c, session, _ = client
        ins = make_multi_report(session, conversation_id="conv-curve-bad")
        repository.save_emotion_session(
            session, "conv-curve-bad", "multi", "t", "张三",
            items=[], summary={"timeline": []}, messages=[], degraded=False,
        )
        d = c.get(f"/api/emotion/inspection/{ins.id}").json()
        assert d["curve"]["degraded"] is True
        assert d["curve"]["stats"]["initial"] is None
