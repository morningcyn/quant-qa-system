# 客户情绪 API：双锚点 / 404 各态 / 幂等 / 批量进度页 current_emotion
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.db import batch_repository as brepo
from backend.db import repository
from backend.db.database import get_db
from backend.db.models import EmotionSession
from backend.main import app

from tests.conftest import MockLLMClient

MULTI_DIALOGUE = (
    "客户 张三 2026-08-01 10:00:00\n有点慌，不知道怎么办\n\n"
    "助理王萌 2026-08-01 10:01:00\n别急，我帮您看看\n\n"
    "客户 张三 2026-08-01 10:02:00\n好的，谢谢"
)


def emotion_items_json(turn_nos, emotion="焦虑", intensity=3, confidence=0.9, trigger="持仓亏损", evidence="有点慌"):
    return json.dumps(
        {
            "items": [
                {"turn_no": t, "emotion": emotion, "intensity": intensity,
                 "confidence": confidence, "trigger": trigger, "evidence": evidence}
                for t in turn_nos
            ]
        },
        ensure_ascii=False,
    )


def dialogue_llm_json():
    """张三会话的标准 mock 输出：turn1 焦虑（有点慌），turn3 中性（好的）。"""
    return json.dumps(
        {
            "items": [
                {"turn_no": 1, "emotion": "焦虑", "intensity": 3, "confidence": 0.9,
                 "trigger": "持仓亏损", "evidence": "有点慌"},
                {"turn_no": 3, "emotion": "中性", "intensity": 0, "confidence": 0.9,
                 "trigger": "未知", "evidence": "好的"},
            ]
        },
        ensure_ascii=False,
    )


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


def make_multi_report(session, conversation_id="conv-api"):
    """多人质检报告 + 总览（消息重建事实源）。"""
    emp = repository.create_assistant(session, "王萌", "E001", "standard")
    ins = repository.save_inspection(session, emp.id, "张三会话", 69, False, [], raw_dialogue="x")
    repository.set_inspection_conversation(session, ins.id, conversation_id)
    repository.save_overview(session, conversation_id, "张三会话", MULTI_DIALOGUE, {"participants": []}, False, [ins.id])
    return ins


class TestEmotionApi:
    def test_analyze_multi_then_get(self, client):
        c, session, llm = client
        ins = make_multi_report(session)
        resp = c.post("/api/emotion/analyze", json={"inspection_id": ins.id})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["conversation_id"] == "conv-api" and data["source_type"] == "multi"
        assert data["customer_name"] == "张三" and data["title"] == "张三会话"
        assert data["current"]["emotion"] == "中性"  # 末条客轮
        assert data["changes"]["total"] == 1
        assert data["timeline"][1]["change"] == "improved"  # 焦虑→中性
        assert data["main_triggers"][0]["trigger"] == "持仓亏损"
        assert data["per_assistant"][0]["assistant_name"] == "王萌"
        assert data["per_assistant"][0]["improve_rate"] == 1.0
        # GET 返回同一份数据
        resp2 = c.get(f"/api/emotion/inspection/{ins.id}")
        assert resp2.status_code == 200
        d2 = resp2.json()
        assert d2["conversation_id"] == "conv-api" and d2["current"]["emotion"] == "中性"

    def test_analyze_batch_anchor(self, client):
        """批量报告：锚点解析到 batch_id:task_id（不走多人质检总览）。"""
        c, session, _ = client
        emp = repository.create_assistant(session, "王萌", "E001", "standard")
        batch_id = "b_api"
        brepo.create_batch_run(session, batch_id, "批", {"customer_count": 1}, [])
        msgs = [
            {"turn_no": 1, "role": "客", "speaker": "客户", "canonical_name": "客户", "text": "有点慌", "timestamp": None, "assistant_id": None, "raw_line": ""},
            {"turn_no": 2, "role": "助", "speaker": "王萌", "canonical_name": "王萌", "text": "别急", "timestamp": None, "assistant_id": emp.id, "raw_line": ""},
            {"turn_no": 3, "role": "客", "speaker": "客户", "canonical_name": "客户", "text": "好的", "timestamp": None, "assistant_id": None, "raw_line": ""},
        ]
        brepo.create_tasks(session, batch_id, [{"task_id": "task_001", "customer_id": "c1", "customer_name": "客户甲", "assistant_ids": [], "input_data": {"messages": msgs, "title": "批会话", "source_fmt": "text"}}])
        ins = repository.save_inspection(session, emp.id, "批会话", 69, False, [])
        repository.set_inspection_conversation(session, ins.id, batch_id)
        task = brepo.get_task(session, batch_id, "task_001")
        brepo.set_task_status(session, task, "completed", result_json={"reports": [{"inspection_id": ins.id, "assistant_name": "王萌"}]})
        resp = c.post("/api/emotion/analyze", json={"inspection_id": ins.id})
        assert resp.status_code == 200, resp.text
        assert resp.json()["conversation_id"] == f"{batch_id}:task_001"
        assert resp.json()["source_type"] == "batch"

    def test_analyze_idempotent_upsert(self, client):
        """重复 POST → 重新分析覆盖，emotion_sessions 仍只有一行。"""
        c, session, llm = client
        ins = make_multi_report(session)
        assert c.post("/api/emotion/analyze", json={"inspection_id": ins.id}).status_code == 200
        llm.responses.append(dialogue_llm_json())  # 第二次分析也需要一次 LLM 调用
        assert c.post("/api/emotion/analyze", json={"inspection_id": ins.id}).status_code == 200
        rows = list(session.scalars(select(EmotionSession)))
        assert len(rows) == 1

    def test_analyze_report_not_found(self, client):
        c, *_ = client
        resp = c.post("/api/emotion/analyze", json={"inspection_id": 999999})
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_get_not_generated(self, client):
        c, session, _ = client
        ins = make_multi_report(session)
        resp = c.get(f"/api/emotion/inspection/{ins.id}")
        assert resp.status_code == 404
        assert resp.json()["code"] == "emotion_not_found"

    def test_get_unsupported_single_assistant(self, client):
        """单助理报告无 conversation_id → 无法定位会话。"""
        c, session, _ = client
        emp = repository.create_assistant(session, "王萌", "E001", "standard")
        ins = repository.save_inspection(session, emp.id, "单助理", 69, False, [])
        resp = c.post("/api/emotion/analyze", json={"inspection_id": ins.id})
        assert resp.status_code == 404
        assert resp.json()["code"] == "emotion_unsupported"

    def test_analyze_no_customer_message_400(self, client):
        """会话无客户消息（纯助理端记录）→ 400 明确提示。"""
        c, session, _ = client
        emp = repository.create_assistant(session, "王萌", "E001", "standard")
        raw = "助理王萌 2026-08-01 10:01:00\n您好\n\n助理王萌 2026-08-01 10:02:00\n再见"
        ins = repository.save_inspection(session, emp.id, "纯助", 69, False, [], raw_dialogue="x")
        repository.set_inspection_conversation(session, ins.id, "conv-no-cust")
        repository.save_overview(session, "conv-no-cust", "纯助", raw, {}, False, [ins.id])
        resp = c.post("/api/emotion/analyze", json={"inspection_id": ins.id})
        assert resp.status_code == 400
        assert resp.json()["code"] == "no_customer_message"
        assert repository.get_emotion_session_by_conversation(session, "conv-no-cust") is None

    def test_llm_failure_400_no_dirty_row(self, client):
        """LLM 失败 → 400（LLMError 全局 handler）；不落任何脏行（批量场景才由调用方降级）。"""
        c, session, llm = client
        ins = make_multi_report(session, conversation_id="conv-fail")
        from backend.services.llm.base import LLMError

        llm.responses.clear()
        llm.responses.append(LLMError("network", "mock 网络错误"))
        resp = c.post("/api/emotion/analyze", json={"inspection_id": ins.id})
        assert resp.status_code == 400
        assert resp.json()["code"] == "network"
        assert session.scalar(select(func.count()).select_from(EmotionSession)) == 0

    def test_batch_progress_includes_current_emotion(self, client):
        """批量进度页：任务行带 current_emotion（completed 任务已自动分析）。"""
        c, session, _ = client
        emp = repository.create_assistant(session, "王萌", "E001", "standard")
        batch_id = "b_prog"
        brepo.create_batch_run(session, batch_id, "批", {"customer_count": 1}, [])
        msgs = [
            {"turn_no": 1, "role": "客", "speaker": "客户", "canonical_name": "客户", "text": "有点慌", "timestamp": None, "assistant_id": None, "raw_line": ""},
            {"turn_no": 2, "role": "助", "speaker": "王萌", "canonical_name": "王萌", "text": "别急", "timestamp": None, "assistant_id": emp.id, "raw_line": ""},
        ]
        brepo.create_tasks(session, batch_id, [{"task_id": "task_001", "customer_id": "c1", "customer_name": "客户甲", "assistant_ids": [], "input_data": {"messages": msgs, "title": "批会话", "source_fmt": "text"}}])
        ins = repository.save_inspection(session, emp.id, "批会话", 69, False, [])
        repository.set_inspection_conversation(session, ins.id, batch_id)
        task = brepo.get_task(session, batch_id, "task_001")
        brepo.set_task_status(
            session, task, "completed",
            result_json={"reports": [{"inspection_id": ins.id, "assistant_name": "王萌", "total_score": 69}], "emotion_id": 1},
        )
        # 模拟批量评分时自动落库的情绪行
        repository.save_emotion_session(
            session, f"{batch_id}:task_001", "batch", "批会话", "客户甲",
            [], {"current": {"emotion": "焦虑", "intensity": 3}}, [], False,
        )
        progress = c.get(f"/api/batch/{batch_id}/progress").json()
        item = progress["items"][0]
        assert item["current_emotion"]["emotion"] == "焦虑"
        assert item["current_emotion"]["intensity"] == 3
