# 情绪分析器：分批 / evidence 逐字校验 / 合成兜底 / 落库 / 双锚点上下文解析
import json

import pytest
from sqlalchemy import select

from backend.db import batch_repository as brepo
from backend.db import repository
from backend.db.models import EmotionSession
from backend.services.emotion.analyzer import (
    _chunk_inputs,
    _finalize_item,
    _rebuild_from_siblings,
    analyze_session,
    resolve_inspection_context,
)
from backend.services.llm.base import LLMError
from backend.services.multiparser import MultiMessage
from backend.utils.errors import BizError

from tests.conftest import MockLLMClient

# 与 conftest session fixture 共用（模板 seed，无员工/无设置）


def mk(role, turn_no, text="好的", assistant_id=None, canonical_name=None):
    if role == "客":
        speaker, canonical = "客户", "客户"
    else:
        canonical = canonical_name or f"助{turn_no}"
        speaker = canonical
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


def emotion_items_json(turn_nos, emotion="中性", intensity=0, confidence=0.9, trigger="未知", evidence="好的"):
    items = [
        {
            "turn_no": t,
            "emotion": emotion,
            "intensity": intensity,
            "confidence": confidence,
            "trigger": trigger,
            "evidence": evidence,
        }
        for t in turn_nos
    ]
    return json.dumps({"items": items}, ensure_ascii=False)


class TestChunkInputs:
    def test_size_based_batches_with_overlap(self):
        batches = _chunk_inputs([(i, "x" * 10) for i in range(1, 100)])
        assert len(batches) == 3
        assert [b[0][0] for b in batches] == [1, 38, 78]
        assert [b[-1][0] for b in batches] == [40, 80, 99]
        assert len(batches[1]) == 43  # 40 + 重叠 3
        assert batches[1][:3] == [(38, "x" * 10), (39, "x" * 10), (40, "x" * 10)]  # 重叠头部=前批尾部

    def test_single_batch_when_small(self):
        batches = _chunk_inputs([(i, "x") for i in range(1, 40)])
        assert len(batches) == 1 and len(batches[0]) == 39

    def test_oversized_lines_each_own_batch(self):
        """单条消息超字符预算 → 每条单独成批（消息不可拆分）；后批并入前批尾部作重叠。"""
        batches = _chunk_inputs([(i, "x" * 15000) for i in (1, 2, 3)])
        assert len(batches) == 3
        assert [b[0][0] for b in batches] == [1, 1, 1]
        assert [b[-1][0] for b in batches] == [1, 2, 3]  # 每批都带上全部前序（重叠=整批）

    def test_empty_input(self):
        assert _chunk_inputs([]) == []


class TestFinalizeItem:
    def test_verbatim_evidence_kept(self):
        out = _finalize_item(
            {"turn_no": 2, "evidence": "天天睡不着觉。"}, "已经亏了快20个点了，天天睡不着觉。"
        )
        assert out["evidence"] == "天天睡不着觉。"
        assert out["evidence_adjusted"] is False
        assert out["synthesized"] is False

    def test_mismatched_evidence_replaced_by_full_text(self):
        """LLM 改写/转述 evidence → 替换为原文全文（绝不写入客户没说过的话）。"""
        out = _finalize_item({"turn_no": 2, "evidence": "客户感到非常焦虑"}, "已经亏了快20个点了。")
        assert out["evidence"] == "已经亏了快20个点了。"
        assert out["evidence_adjusted"] is True

    def test_empty_evidence_replaced(self):
        out = _finalize_item({"turn_no": 2, "evidence": "  "}, "原文内容")
        assert out["evidence"] == "原文内容" and out["evidence_adjusted"] is True

    def test_whitespace_stripped_before_match(self):
        out = _finalize_item({"turn_no": 2, "evidence": "  天天睡不着觉。  "}, "已经亏了快20个点了，天天睡不着觉。")
        assert out["evidence_adjusted"] is False and out["evidence"] == "天天睡不着觉。"


def run_analyze(session, msgs, client, conversation_id="multi-x", **over):
    import asyncio

    return asyncio.run(
        analyze_session(
            session,
            msgs=msgs,
            title="测试会话",
            conversation_id=conversation_id,
            source_type="multi",
            customer_name=None,
            client=client,
            cfg={},
            **over,
        )
    )


class TestAnalyzeSession:
    def test_happy_path_persists(self, session):
        msgs = [mk("客", 1, "有点慌"), mk("助", 2, "别慌", 1, "王萌"), mk("客", 3, "好的")]
        # turn1 焦虑 / turn3 中性（各自 evidence 命中原文）
        client = MockLLMClient(
            [
                json.dumps(
                    {
                        "items": [
                            {"turn_no": 1, "emotion": "焦虑", "intensity": 3, "confidence": 0.9, "trigger": "持仓亏损", "evidence": "有点慌"},
                            {"turn_no": 3, "emotion": "中性", "intensity": 0, "confidence": 0.9, "trigger": "未知", "evidence": "好的"},
                        ]
                    },
                    ensure_ascii=False,
                )
            ]
        )
        emo = run_analyze(session, msgs, client)
        assert emo is not None
        row = repository.get_emotion_session_by_conversation(session, "multi-x")
        assert row is not None and row.conversation_id == "multi-x"
        assert row.source_type == "multi" and row.title == "测试会话"
        items = json.loads(row.items_json)
        assert len(items) == 2
        assert {i["turn_no"] for i in items} == {1, 3}
        assert items[0]["emotion"] == "焦虑" and items[0]["evidence"] == "有点慌"
        summary = json.loads(row.summary_json)
        assert [t["change"] for t in summary["timeline"]] == [None, "improved"]  # 焦虑→中性
        assert summary["changes"]["improved"] == 1
        assert summary["current"]["emotion"] == "中性"
        assert client.calls[0]["temperature"] == 0.1  # 低温度保证确定性

    def test_multi_batch_overlap_later_wins(self, session):
        """41 条客轮 → 2 次 LLM 调用；重叠区（38-40）后批输出覆盖前批。"""
        msgs = [mk("客", i, f"消息{i} 好的") for i in range(1, 42)]
        # 响应1：1-40 全"担忧"；响应2：38-41（turn 40 → 中性，重叠区后批覆盖）
        client = MockLLMClient(
            [
                emotion_items_json(range(1, 41), emotion="担忧", intensity=2, evidence="好的"),
                emotion_items_json([38, 39, 40, 41], emotion="中性", intensity=0, evidence="好的"),
            ]
        )
        run_analyze(session, msgs, client)
        assert len(client.calls) == 2
        row = repository.get_emotion_session_by_conversation(session, "multi-x")
        items = {i["turn_no"]: i for i in json.loads(row.items_json)}
        assert len(items) == 41
        assert items[40]["emotion"] == "中性"  # 后批覆盖前批
        assert items[41]["emotion"] == "中性"
        assert items[1]["emotion"] == "担忧"

    def test_missing_turns_synthesized_neutral(self, session):
        """LLM 漏标 turn 2 → 确定性合成中性（confidence=0.0，落入低置信度）。"""
        msgs = [mk("客", 1), mk("客", 2), mk("客", 3)]
        client = MockLLMClient([emotion_items_json([1, 3], emotion="担忧")])
        run_analyze(session, msgs, client)
        row = repository.get_emotion_session_by_conversation(session, "multi-x")
        items = {i["turn_no"]: i for i in json.loads(row.items_json)}
        assert items[2]["emotion"] == "中性"
        assert items[2]["intensity"] == 0 and items[2]["confidence"] == 0.0
        assert items[2]["synthesized"] is True
        assert json.loads(row.summary_json)["low_confidence_count"] == 1

    def test_empty_text_customer_skips_llm(self, session):
        """空文本客轮不进 LLM（合成中性），LLM 只收到非空客轮。"""
        msgs = [mk("客", 1, ""), mk("客", 2, "有文本")]
        client = MockLLMClient([emotion_items_json([2], emotion="中性")])
        run_analyze(session, msgs, client)
        assert len(client.calls) == 1
        assert "[1][客户]" not in client.calls[0]["user"]
        row = repository.get_emotion_session_by_conversation(session, "multi-x")
        items = {i["turn_no"]: i for i in json.loads(row.items_json)}
        assert items[1]["synthesized"] is True and items[1]["emotion"] == "中性"

    def test_no_customer_messages_returns_none(self, session):
        msgs = [mk("助", 1, "您好", 1, "王萌")]
        assert run_analyze(session, msgs, MockLLMClient([])) is None
        assert repository.get_emotion_session_by_conversation(session, "multi-x") is None

    def test_llm_failure_raises(self, session):
        msgs = [mk("客", 1, "你好")]
        with pytest.raises(LLMError):
            run_analyze(session, msgs, MockLLMClient([LLMError("network", "mock 失败")]))
        # 失败不落库
        assert repository.get_emotion_session_by_conversation(session, "multi-x") is None

    def test_upsert_overwrites(self, session):
        """同锚点重复分析 → 覆盖旧结果（UNIQUE 不冲突）。"""
        msgs = [mk("客", 1, "你好")]
        run_analyze(session, msgs, MockLLMClient([emotion_items_json([1], emotion="担忧")]))
        run_analyze(session, msgs, MockLLMClient([emotion_items_json([1], emotion="中性")]))
        rows = list(session.scalars(select(EmotionSession)))
        assert len(rows) == 1
        assert json.loads(rows[0].items_json)[0]["emotion"] == "中性"

    def test_degraded_warning_flag(self, session):
        msgs = [mk("客", 1, "你好")]
        emo = run_analyze(session, msgs, MockLLMClient([emotion_items_json([1])]), warning="降级重建")
        assert emo.degraded is True and emo.warning == "降级重建"


class TestRebuildFromSiblings:
    def test_merge_dedupe_and_multiline(self, session):
        """兄弟报告 raw_dialogue（编号文本）合并：按 turn_no 去重、续行拼接、角色判定。"""
        emp = repository.create_assistant(session, "王萌", "E001", "standard")
        a = repository.save_inspection(
            session, emp.id, "会话A", 69, False, [],
            raw_dialogue="[1][客户] 你好\n[2][王萌] 您好\n这是续行\n[3][客户] 谢谢",
        )
        # 报告 B 与 A 有重叠 turn（setdefault 保留首个）+ 新增 turn 4
        b = repository.save_inspection(
            session, emp.id, "会话A", 70, False, [],
            raw_dialogue="[3][客户] 谢谢（重复）\n[4][王萌] 不客气",
        )
        repository.set_inspection_conversation(session, a.id, "conv-sib")
        repository.set_inspection_conversation(session, b.id, "conv-sib")
        ins = repository.get_inspection(session, a.id)
        msgs, warning = _rebuild_from_siblings(session, ins, "conv-sib")
        assert warning and "重建" in warning
        assert [(m.turn_no, m.role, m.text) for m in msgs] == [
            (1, "客", "你好"),
            (2, "助", "您好\n这是续行"),
            (3, "客", "谢谢"),
            (4, "助", "不客气"),
        ]
        assert msgs[1].canonical_name == "王萌"

    def test_skips_reports_without_detail(self, session):
        emp = repository.create_assistant(session, "王萌", "E001", "standard")
        a = repository.save_inspection(session, emp.id, "A", 69, False, [], raw_dialogue="[1][客户] 你好")
        repository.set_inspection_conversation(session, a.id, "conv-x")
        # 无 detail 的孤儿报告（手动构造，补 NOT NULL 列）
        from backend.db.models import Inspection

        orphan = Inspection(
            assistant_id=emp.id, total_score=0, is_yellow_alert=False, conversation_id="conv-x",
            template_snapshot_json="{}", yellow_alert_reasons_json="[]", red_alert_reasons_json="[]",
            na_dims_json="[]", template_type="standard",
        )
        session.add(orphan)
        session.commit()
        msgs, _ = _rebuild_from_siblings(session, repository.get_inspection(session, a.id), "conv-x")
        assert [(m.turn_no, m.role) for m in msgs] == [(1, "客")]


class TestResolveContext:
    def test_no_conversation_id_unsupported(self, session):
        emp = repository.create_assistant(session, "王萌", "E001", "standard")
        ins = repository.save_inspection(session, emp.id, "单助理", 69, False, [])
        with pytest.raises(BizError) as e:
            resolve_inspection_context(session, ins)
        assert e.value.code == "emotion_unsupported"

    def test_multi_with_overview(self, session, monkeypatch):
        """多人质检：conversation_id=会话锚点，有总览 → 用总览原始记录重建（parse_multi）。"""
        monkeypatch.setattr("backend.services.emotion.analyzer.multiparser.load_name_map", lambda: {})
        monkeypatch.setattr("backend.services.emotion.analyzer.multiparser.load_not_assistant_names", lambda: [])
        emp = repository.create_assistant(session, "王萌", "E001", "standard")
        raw = (
            "客户 张三 2026-08-01 10:00:00\n你好\n\n"
            "助理王萌 2026-08-01 10:01:00\n您好\n\n"
            "客户 张三 2026-08-01 10:02:00\n谢谢"
        )
        ins = repository.save_inspection(session, emp.id, "张三会话", 69, False, [], raw_dialogue="x")
        repository.set_inspection_conversation(session, ins.id, "conv-m")
        repository.save_overview(session, "conv-m", "张三会话", raw, {"participants": []}, False, [ins.id])
        ctx = resolve_inspection_context(session, repository.get_inspection(session, ins.id))
        assert ctx["conversation_id"] == "conv-m" and ctx["source_type"] == "multi"
        assert ctx["title"] == "张三会话" and ctx["customer_name"] == "张三"
        roles = [getattr(m, "role", None) for m in ctx["msgs"]]
        assert roles == ["客", "助", "客"]
        # 助轮完成员工匹配（assistant_id 填充）
        asst = [m for m in ctx["msgs"] if getattr(m, "role", None) == "助"][0]
        assert asst.assistant_id == emp.id

    def test_multi_without_overview_sibling_rebuild(self, session):
        emp = repository.create_assistant(session, "王萌", "E001", "standard")
        a = repository.save_inspection(
            session, emp.id, "会话X", 69, False, [],
            raw_dialogue="[1][客户] 你好\n[2][王萌] 您好",
        )
        repository.set_inspection_conversation(session, a.id, "conv-sib2")
        ctx = resolve_inspection_context(session, repository.get_inspection(session, a.id))
        assert ctx["warning"] is not None
        assert [(m.turn_no, m.role) for m in ctx["msgs"]] == [(1, "客"), (2, "助")]
        assert ctx["customer_name"] is None  # 无昵称（speaker=客户）

    def test_batch_anchor(self, session):
        """批量模式：conversation_id=batch_id → 反查任务 → 锚点 batch_id:task_id，消息从 input_data 重建。"""
        emp = repository.create_assistant(session, "王萌", "E001", "standard")
        batch_id = "b_resolve"
        brepo.create_batch_run(session, batch_id, "批", {"customer_count": 1}, [])
        msg_dicts = [
            {"turn_no": 1, "role": "客", "speaker": "客户", "canonical_name": "客户", "text": "你好", "timestamp": None, "assistant_id": None, "raw_line": ""},
            {"turn_no": 2, "role": "助", "speaker": "王萌", "canonical_name": "王萌", "text": "您好", "timestamp": None, "assistant_id": emp.id, "raw_line": ""},
        ]
        brepo.create_tasks(
            session, batch_id,
            [{"task_id": "task_001", "customer_id": "c1", "customer_name": "客户甲", "assistant_ids": [],
              "input_data": {"messages": msg_dicts, "title": "批会话", "source_fmt": "text"}}],
        )
        ins = repository.save_inspection(session, emp.id, "批会话", 69, False, [])
        repository.set_inspection_conversation(session, ins.id, batch_id)
        task = brepo.get_task(session, batch_id, "task_001")
        brepo.set_task_status(
            session, task, "completed",
            result_json={"reports": [{"inspection_id": ins.id, "assistant_name": "王萌"}]},
        )
        ctx = resolve_inspection_context(session, repository.get_inspection(session, ins.id))
        assert ctx["conversation_id"] == f"{batch_id}:task_001"
        assert ctx["source_type"] == "batch" and ctx["customer_name"] == "客户甲"
        assert [getattr(m, "role", None) for m in ctx["msgs"]] == ["客", "助"]
        asst = [m for m in ctx["msgs"] if getattr(m, "role", None) == "助"][0]
        assert asst.assistant_id == emp.id  # input_data 已带员工匹配结果

    def test_batch_old_data_no_task_match_unsupported(self, session):
        """批量老数据：任务结果里找不到该报告 → emotion_unsupported。"""
        emp = repository.create_assistant(session, "王萌", "E001", "standard")
        batch_id = "b_old"
        brepo.create_batch_run(session, batch_id, "批", {"customer_count": 1}, [])
        brepo.create_tasks(session, batch_id, [{"task_id": "task_001", "customer_id": "c1", "customer_name": "客户甲", "assistant_ids": [], "input_data": {"messages": [], "title": None, "source_fmt": "text"}}])
        ins = repository.save_inspection(session, emp.id, "x", 69, False, [])
        repository.set_inspection_conversation(session, ins.id, batch_id)
        task = brepo.get_task(session, batch_id, "task_001")
        brepo.set_task_status(session, task, "completed", result_json={"reports": [{"inspection_id": 999999}]})
        with pytest.raises(BizError) as e:
            resolve_inspection_context(session, repository.get_inspection(session, ins.id))
        assert e.value.code == "emotion_unsupported"

    def test_need_messages_false_skips_rebuild(self, session, monkeypatch):
        """need_messages=False：只解析锚点不重建消息（GET 查询路径）。"""
        monkeypatch.setattr("backend.services.emotion.analyzer.multiparser.load_name_map", lambda: {})
        monkeypatch.setattr("backend.services.emotion.analyzer.multiparser.load_not_assistant_names", lambda: [])
        emp = repository.create_assistant(session, "王萌", "E001", "standard")
        raw = "客户 张三 2026-08-01 10:00:00\n你好\n\n王萌 2026-08-01 10:01:00\n您好"
        ins = repository.save_inspection(session, emp.id, "张", 69, False, [], raw_dialogue="x")
        repository.set_inspection_conversation(session, ins.id, "conv-nm")
        repository.save_overview(session, "conv-nm", "张", raw, {}, False, [ins.id])
        ctx = resolve_inspection_context(session, repository.get_inspection(session, ins.id), need_messages=False)
        assert ctx["msgs"] == [] and ctx["conversation_id"] == "conv-nm"
