# 多人质检分发器：并发调用单助理评分 → 各助理报告 + 本次服务总览（含规则降级）
import asyncio
import json

import pytest

from backend.db import repository
from backend.services import dispatcher
from backend.utils.errors import BizError
from tests.conftest import (
    MULTI_SAMPLE_DIALOGUE,
    MockLLMByUserClient,
    LLMError,
    overview_llm_json,
    scored_llm_json,
    valid_llm_json,
)


def _turn(payload: str, turn: int) -> str:
    """修改 mock 输出中 highlight 的轮次号（验证绝对轮次保留）。"""
    data = json.loads(payload)
    data["highlight_dialogue"][0]["turn"] = turn
    return json.dumps(data, ensure_ascii=False)


@pytest.fixture()
def two_assistants(session):
    a1 = repository.create_assistant(session, "王萌", "E001", "standard")
    a2 = repository.create_assistant(session, "徐艺桐", "E002", "standard")
    return {"王萌": a1.id, "徐艺桐": a2.id}


class TestRunMultiInspection:
    def test_happy_path_two_reports_plus_overview(self, session, two_assistants):
        client = MockLLMByUserClient(
            [
                ("请按系统提示输出总览 JSON", overview_llm_json()),
                ("本次评估对象：王萌", _turn(valid_llm_json(69), 2)),
                ("本次评估对象：徐艺桐", _turn(scored_llm_json(59), 4)),
            ]
        )
        out = asyncio.run(
            dispatcher.run_multi_inspection(session, MULTI_SAMPLE_DIALOGUE, "宇树客户服务", two_assistants, client=client, cfg={})
        )
        assert out["errors"] == []
        assert out["conversation_id"] and out["overview_id"]
        assert sorted(r["assistant_name"] for r in out["reports"]) == ["徐艺桐", "王萌"]
        # 每份报告 evaluatee=本人、conversation_id 已回写、highlight 轮次=原始绝对轮次（可追溯）
        for r in out["reports"]:
            assert r["evaluatee"] == r["assistant_name"]
            assert r["reply_count"] == 1
            ins = repository.get_inspection(session, r["id"])
            assert ins.conversation_id == out["conversation_id"]
        wang = next(r for r in out["reports"] if r["assistant_name"] == "王萌")
        xu = next(r for r in out["reports"] if r["assistant_name"] == "徐艺桐")
        assert wang["highlight_dialogue"][0]["turn"] == 2
        assert xu["highlight_dialogue"][0]["turn"] == 4
        assert wang["total_score"] == 69 and xu["total_score"] == 59
        # 上下文注入：王萌段带块尾后客户轮（仅参考），徐艺桐段带段前前文
        wang_call = next(c for c in client.calls if "本次评估对象：王萌" in c["user"])
        assert "【上下文（评估对象发言之外的对话，仅作参考衔接理解，不计分）】" in wang_call["user"]
        assert "宇树现在跌这么多了" in wang_call["user"]  # 后文客户轮在上下文区（不在正文计分段）
        xu_call = next(c for c in client.calls if "本次评估对象：徐艺桐" in c["user"])
        assert "您在机构圈子里解读的股票我天天看" in xu_call["user"]  # 前文在上下文区
        # 总览：非降级、LLM 汇总落库、完整原始聊天记录保留
        ov = repository.get_overview(session, out["overview_id"])
        assert ov is not None and ov.degraded is False
        data = json.loads(ov.summary_json)
        assert data["summary"]["customer_issue_resolved"] == "是"
        assert len(data["participants"]) == 2
        assert ov.raw_dialogue == MULTI_SAMPLE_DIALOGUE.strip()
        assert "哈尔滨赢家1122" in ov.raw_dialogue

    def test_partial_failure_degraded_overview(self, session, two_assistants):
        client = MockLLMByUserClient(
            [
                ("请按系统提示输出总览 JSON", LLMError("bad_json", "mock 总览失败")),
                ("本次评估对象：王萌", valid_llm_json(69)),
                ("本次评估对象：徐艺桐", LLMError("auth", "key 无效")),
            ]
        )
        out = asyncio.run(
            dispatcher.run_multi_inspection(session, MULTI_SAMPLE_DIALOGUE, "t", two_assistants, client=client, cfg={})
        )
        assert len(out["reports"]) == 1 and out["reports"][0]["assistant_name"] == "王萌"
        assert len(out["errors"]) == 1
        assert out["errors"][0]["canonical_name"] == "徐艺桐"
        assert out["errors"][0]["code"] == "auth"
        ov = repository.get_overview(session, out["overview_id"])
        assert ov.degraded is True
        data = json.loads(ov.summary_json)
        assert "规则自动生成" in data["summary"]["overall_comment"]
        assert data["summary"]["customer_issue_resolved"] == "无法判断"

    def test_all_failed_raises(self, session, two_assistants):
        client = MockLLMByUserClient(
            [
                ("本次评估对象：王萌", LLMError("auth", "x")),
                ("本次评估对象：徐艺桐", LLMError("bad_json", "y")),
            ]
        )
        with pytest.raises(BizError) as excinfo:
            asyncio.run(
                dispatcher.run_multi_inspection(session, MULTI_SAMPLE_DIALOGUE, "t", two_assistants, client=client, cfg={})
            )
        assert excinfo.value.code == "multi_all_failed"

    def test_missing_mapping_raises(self, session, two_assistants):
        with pytest.raises(BizError) as excinfo:
            asyncio.run(
                dispatcher.run_multi_inspection(session, MULTI_SAMPLE_DIALOGUE, "t", {"王萌": two_assistants["王萌"]}, client=MockLLMByUserClient([]), cfg={})
            )
        assert excinfo.value.code == "unmapped_assistant"
        assert "徐艺桐" in excinfo.value.message

    def test_extra_mapping_key_raises(self, session, two_assistants):
        mapping = dict(two_assistants, **{"张三": two_assistants["王萌"]})
        with pytest.raises(BizError) as excinfo:
            asyncio.run(
                dispatcher.run_multi_inspection(session, MULTI_SAMPLE_DIALOGUE, "t", mapping, client=MockLLMByUserClient([]), cfg={})
            )
        assert excinfo.value.code == "validation_error"

    def test_unknown_assistant_id_404(self, session, two_assistants):
        mapping = {"王萌": 9999, "徐艺桐": two_assistants["徐艺桐"]}
        with pytest.raises(BizError) as excinfo:
            asyncio.run(
                dispatcher.run_multi_inspection(session, MULTI_SAMPLE_DIALOGUE, "t", mapping, client=MockLLMByUserClient([]), cfg={})
            )
        assert excinfo.value.status_code == 404

    def test_single_assistant_batch(self, session):
        a1 = repository.create_assistant(session, "王萌", "E001", "standard")
        raw = "客户 张三 2026-08-24 10:00:00\n你好\n助理王萌\n2026-08-24 10:01:00\n您好，请讲\n客户 张三\n2026-08-24 10:02:00\n谢谢"
        client = MockLLMByUserClient(
            [
                ("请按系统提示输出总览 JSON", overview_llm_json()),
                ("本次评估对象：王萌", valid_llm_json()),
            ]
        )
        out = asyncio.run(
            dispatcher.run_multi_inspection(session, raw, "单人", {"王萌": a1.id}, client=client, cfg={})
        )
        assert len(out["reports"]) == 1 and out["errors"] == []
        assert out["reports"][0]["reply_count"] == 1

    def test_three_line_display_name_same_name_map_as_preview(self, session, monkeypatch):
        """预览与提交一致性：三行式显示名记录，dispatcher 必须与预览用同一份 name_map。

        回归：dispatcher 原先不加载 name_map → 后端重新解析出"韩珂龙头班"簇，
        mapping["段勇亮"] 对不上 → 误报"以下助理尚未指定归属员工"。"""
        from backend.services import multiparser

        raw = (
            "邯郸赢家0878\n2026-07-03 13:12:42\n你好韩老师！300166提醒加仓没看到现在能加吗？\n\n"
            "韩珂龙头班\n2026-07-03 14:32:24\n可以按照中线模式低吸加仓5%\n\n"
            "邯郸赢家0878\n2026-07-10 13:31:50\n韩老师好！000420现在能加仓吗？"
        )
        dyl = repository.create_assistant(session, "段勇亮", "E003", "standard")
        monkeypatch.setattr(multiparser, "load_name_map", lambda: {"韩珂龙头班": "段勇亮"})

        # 预览视角（parse.preview_multi 同款）：canonical_name = 段勇亮（员工匹配）
        prev = multiparser.parse_multi(raw, repository.list_assistants(session), multiparser.load_name_map())
        assert [c.canonical_name for c in prev.clusters] == ["段勇亮"]

        # 提交视角：dispatcher 内部加载同一份 name_map → mapping 覆盖，不再误报未归属
        client = MockLLMByUserClient(
            [
                ("请按系统提示输出总览 JSON", overview_llm_json()),
                ("本次评估对象：段勇亮", valid_llm_json()),
            ]
        )
        out = asyncio.run(
            dispatcher.run_multi_inspection(session, raw, "t", {"段勇亮": dyl.id}, client=client, cfg={})
        )
        assert out["errors"] == []
        assert out["reports"][0]["assistant_name"] == "段勇亮"

    def test_overview_fallback_uses_red_reasons(self, session, two_assistants):
        """规则降级：红灯根因进入主要问题，且判定为"否"。"""
        client = MockLLMByUserClient(
            [
                ("请按系统提示输出总览 JSON", LLMError("bad_json", "x")),
                ("本次评估对象：王萌", valid_llm_json(red=True, red_reasons=["承诺收益：保准回本"])),
                ("本次评估对象：徐艺桐", valid_llm_json(59)),
            ]
        )
        out = asyncio.run(
            dispatcher.run_multi_inspection(session, MULTI_SAMPLE_DIALOGUE, "t", two_assistants, client=client, cfg={})
        )
        ov = repository.get_overview(session, out["overview_id"])
        data = json.loads(ov.summary_json)
        assert data["summary"]["customer_issue_resolved"] == "否"
        assert any("保准回本" in i for i in data["summary"]["main_issues"])


class TestRepositoryMulti:
    def test_conversation_backfill_and_overview_roundtrip(self, session):
        ast = repository.create_assistant(session, "王萌", "E001", "standard")
        ins = repository.save_inspection(
            session, assistant_id=ast.id, session_title=None, total_score=69,
            is_yellow_alert=False, yellow_alert_reasons=[], template_snapshot={},
            raw_dialogue="[客] 你好\n[助] 您好",
        )
        repository.set_inspection_conversation(session, ins.id, "abc123")
        assert repository.get_inspection(session, ins.id).conversation_id == "abc123"
        ov = repository.save_overview(
            session, "abc123", "标题", "完整原始文本", {"summary": {"main_strengths": ["好"]}, "degraded": False}, False, [ins.id]
        )
        got = repository.get_overview(session, ov.id)
        assert got.conversation_id == "abc123"
        assert json.loads(got.summary_json)["summary"]["main_strengths"] == ["好"]
        assert got.raw_dialogue == "完整原始文本"
