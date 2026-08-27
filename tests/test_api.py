import pytest
from fastapi.testclient import TestClient

from backend.db.database import get_db
from backend.main import app
from tests.conftest import SAMPLE_DIALOGUE


@pytest.fixture()
def client(session):
    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


class TestAssistantsApi:
    def test_crud_and_conflict(self, client):
        resp = client.post("/api/assistants", json={"name": "张三", "employee_no": "E001", "template_type": "standard"})
        assert resp.status_code == 201
        aid = resp.json()["id"]
        resp = client.post("/api/assistants", json={"name": "李四", "employee_no": "E001"})
        assert resp.status_code == 409
        assert resp.json()["code"] == "employee_no_conflict"
        resp = client.get("/api/assistants")
        assert resp.json()["assistants"][0]["employee_no"] == "E001"
        resp = client.put(f"/api/assistants/{aid}", json={"name": "张三丰", "template_type": "vip"})
        assert resp.json()["name"] == "张三丰"
        resp = client.delete(f"/api/assistants/{aid}")
        assert resp.status_code == 204
        resp = client.get(f"/api/assistants/{aid}")
        assert resp.status_code == 404


class TestParseApi:
    def test_preview(self, client):
        resp = client.post("/api/parse/preview", json={"raw_text": SAMPLE_DIALOGUE})
        assert resp.status_code == 200
        data = resp.json()
        assert data["role_stats"]["total"] == 8
        assert data["fmt"] == "text"

    def test_preview_speakers(self, client):
        resp = client.post("/api/parse/preview", json={"raw_text": SAMPLE_DIALOGUE})
        data = resp.json()
        assert data["speakers"] == ["助理A"]  # 助侧显示名（供评估对象下拉）
        assert data["turns"][0]["speaker"] == "客户"
        assert data["turns"][1]["speaker"] == "助理A"

    def test_preview_multi_assistant_speakers(self, client):
        resp = client.post(
            "/api/parse/preview",
            json={"raw_text": "[客服A] 您好\n[客户] 你好\n[客服B] 我在"},
        )
        data = resp.json()
        assert data["speakers"] == ["客服A", "客服B"]
        assert data["turns"][0]["speaker"] == "客服A"
        assert any("多位助理" in w for w in data["warnings"])

    def test_preview_garbage(self, client):
        resp = client.post("/api/parse/preview", json={"raw_text": "没有角色标记的文本"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "parse_failed"


class TestInspectionApi:
    def test_create_inspection_with_evaluatee(self, client, session, monkeypatch):
        """POST 质检携带 evaluatee：注入用户提示词并随报告返回。"""
        from backend.db import repository
        from backend.services.llm import factory as llm_factory
        from tests.conftest import MockLLMClient, valid_llm_json

        assistant = repository.create_assistant(session, "张三", "E001", "standard")
        mock = MockLLMClient([valid_llm_json()])
        monkeypatch.setattr(llm_factory, "get_active_runtime", lambda s: (mock, {}))
        resp = client.post(
            f"/api/assistants/{assistant.id}/inspections",
            json={"session_title": "带评估对象", "raw_dialogue": SAMPLE_DIALOGUE, "evaluatee": "助理A"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["evaluatee"] == "助理A"
        assert "本次评估对象：助理A" in mock.calls[0]["user"]


class TestMultiBatchApi:
    def test_preview_multi_matches_employee(self, client, session):
        from backend.db import repository

        repository.create_assistant(session, "王萌", "E001", "standard")
        resp = client.post(
            "/api/parse/preview-multi",
            json={"raw_text": "[客] 你好\n[王萌] 您好\n[客] 谢谢"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["role_stats"] == {"客": 2, "助": 1, "total": 3}
        assert data["messages"][1]["assistant_id"] == 1
        assert data["assistants"][0]["matched_assistant_id"] == 1
        assert data["assistants"][0]["display_name"] == "王萌"

    def test_batch_happy_path_and_overview_readback(self, client, session, monkeypatch):
        from backend.db import repository
        from backend.services.llm import factory as llm_factory
        from tests.conftest import (
            MULTI_SAMPLE_DIALOGUE,
            MockLLMByUserClient,
            overview_llm_json,
            scored_llm_json,
            valid_llm_json,
        )

        a1 = repository.create_assistant(session, "王萌", "E001", "standard")
        a2 = repository.create_assistant(session, "徐艺桐", "E002", "standard")
        mock = MockLLMByUserClient(
            [
                ("请按系统提示输出总览 JSON", overview_llm_json()),
                ("本次评估对象：王萌", valid_llm_json(69)),
                ("本次评估对象：徐艺桐", scored_llm_json(59)),
            ]
        )
        monkeypatch.setattr(llm_factory, "get_active_runtime", lambda s: (mock, {}))
        resp = client.post(
            "/api/inspections/batch",
            json={
                "raw_dialogue": MULTI_SAMPLE_DIALOGUE,
                "session_title": "宇树客户服务",
                "mapping": {"王萌": a1.id, "徐艺桐": a2.id},
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert len(data["reports"]) == 2 and data["errors"] == []
        assert data["conversation_id"] and data["overview_id"]
        # 总览回读：参与者带报告视图、完整原始记录
        resp2 = client.get(f"/api/overviews/{data['overview_id']}")
        assert resp2.status_code == 200
        ov = resp2.json()
        assert ov["summary"]["customer_issue_resolved"] == "是"
        assert ov["degraded"] is False
        assert len(ov["participants"]) == 2
        assert {p["report"]["total_score"] for p in ov["participants"]} == {69, 59}
        # 每位参与者实时挂载「可借鉴」优点（确定性推导，不依赖落库字段）
        for p in ov["participants"]:
            assert isinstance(p["strengths"], list) and p["strengths"]
        assert "哈尔滨赢家1122" in ov["raw_dialogue"]

    def test_batch_missing_mapping_400(self, client, session):
        from backend.db import repository

        a1 = repository.create_assistant(session, "王萌", "E001", "standard")
        resp = client.post(
            "/api/inspections/batch",
            json={
                "raw_dialogue": "[客] Q1\n[王萌] A1\n[客] F1\n[徐艺桐] B1",
                "mapping": {"王萌": a1.id},
            },
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "unmapped_assistant"

    def test_overview_not_found(self, client):
        resp = client.get("/api/overviews/9999")
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_overview_list_ordered_and_fields(self, client, session):
        """历史总览列表：倒序、字段齐全、空标题回退、participant_count 解析。"""
        from backend.db import repository

        repository.save_overview(
            session, "conv-a", "第一次质检", "raw", {"summary": {}, "participants": [{"name": "王萌"}, {"name": "徐艺桐"}], "degraded": False},
            False, [1],
        )
        repository.save_overview(
            session, "conv-b", "", "raw", {"summary": {}, "participants": [], "degraded": True},
            True, [],
        )
        resp = client.get("/api/overviews")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert data["items"][0]["id"] > data["items"][1]["id"]  # 新生成的在前
        # 先创建：conv-a（2 人、非降级）；后创建：conv-b（0 人、降级、空标题回退）
        assert data["items"][0]["participant_count"] == 0
        assert data["items"][0]["degraded"] is True
        assert data["items"][0]["title"] == "未命名会话"
        assert data["items"][1]["participant_count"] == 2
        assert data["items"][1]["degraded"] is False
        # 分页
        resp = client.get("/api/overviews?page=2&page_size=1")
        assert len(resp.json()["items"]) == 1
        resp = client.get("/api/overviews?page_size=999")
        assert resp.json()["page_size"] == 50  # 上限收敛


class TestSettingsApi:
    def test_model_lifecycle_masked(self, client):
        resp = client.post(
            "/api/settings/models",
            json={
                "name": "DeepSeek",
                "protocol": "openai_compat",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-secret-123",
                "model_name": "deepseek-chat",
                "temperature": 0.2,
            },
        )
        assert resp.status_code == 201
        saved = resp.json()
        assert "sk-secret-123" not in resp.text
        assert saved["has_api_key"] is True
        model_id = saved["id"]
        resp = client.get("/api/settings/models")
        assert "sk-secret-123" not in resp.text
        assert resp.json()["active_model_id"] == model_id  # 首个自动设为默认
        # 编辑留空 key = 保留旧密钥
        resp = client.post(
            "/api/settings/models",
            json={"id": model_id, "name": "DeepSeek2", "protocol": "openai_compat", "base_url": "https://api.deepseek.com/v1", "api_key": "", "model_name": "deepseek-chat"},
        )
        assert resp.json()["has_api_key"] is True
        # 删除
        resp = client.delete(f"/api/settings/models/{model_id}")
        assert resp.status_code == 204
        resp = client.get("/api/settings/models")
        assert resp.json()["models"] == []

    def test_template_validation(self, client):
        resp = client.get("/api/settings/templates")
        config = resp.json()["templates"][0]["config"]
        config["d"]["d1"]["max"] = 9  # 破坏 D 合计
        resp = client.put("/api/settings/templates/standard", json={"name": "x", "config": config})
        assert resp.status_code == 400
        assert "D 端" in resp.json()["message"]
