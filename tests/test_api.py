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
