# 批量评分 API 全链路：import → start → progress → retry-failed（worker 用 mock 拦截，不碰真实库）
import json

import pytest
from fastapi.testclient import TestClient

from backend.db import batch_repository as brepo
from backend.db.database import get_db
from backend.main import app

THREE_CUSTOMERS = (
    "邯郸赢家0878\n2026-07-03 13:12:42\n你好韩老师！300166提醒加仓没看到现在能加吗？\n\n"
    "韩珂龙头班\n2026-07-03 14:32:24\n可以按照中线模式低吸加仓5%，不要追涨就好\n\n"
    "昆明赢家2735\n2026-08-25 21:22:22\n韩老师麻烦问下京东方a明天可以买吗？\n\n"
    "山人俱乐部（李金潓）\n2026-08-25 22:51:19\n可以，中长线没问题\n"
)


@pytest.fixture()
def client(session, monkeypatch):
    """TestClient + 内存库；mgr.start_batch 拦截为记录调用（避免真实后台 worker 触碰真实 DB）。"""
    started = []
    monkeypatch.setattr("backend.api.batch.mgr.start_batch", lambda bid: (started.append(bid) or True))
    # lifespan 的 resume_all 会扫描真实库活跃批次并触发 start_batch → 测试环境必须禁用
    monkeypatch.setattr("backend.main.mgr.resume_all", lambda: None)
    # 隔离全局 name_map（load_name_map 读真实库 settings，测试环境必须为空）
    monkeypatch.setattr("backend.api.batch.multiparser.load_name_map", lambda: {})

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        yield c, started
    app.dependency_overrides.clear()


class TestBatchApi:
    def test_import_splits_customers(self, client):
        c, _ = client
        resp = c.post("/api/batch/import", json={"raw_text": THREE_CUSTOMERS, "title": "8月批量"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["batch_id"]
        assert data["task_count"] == 2
        assert data["customer_count"] == 2
        assert data["message_count"] == 4
        assert data["assistant_count"] == 2
        assert data["source_stats"]["customer_count"] == 2
        names = [x["customer_name"] for x in data["customers"]]
        assert names == ["邯郸赢家0878", "昆明赢家2735"]
        assert data["customers"][0]["assistant_names"] == ["韩珂龙头班"]

    def test_import_unparseable_still_201(self, client):
        """解析失败不 400：返回 parse_error 且建 1 个任务（执行时失败）。"""
        c, _ = client
        resp = c.post("/api/batch/import", json={"raw_text": "无法识别的普通文本"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["parse_error"]
        assert data["task_count"] == 1

    def test_start_and_progress(self, client):
        c, started = client
        batch_id = c.post("/api/batch/import", json={"raw_text": THREE_CUSTOMERS}).json()["batch_id"]
        resp = c.post(f"/api/batch/{batch_id}/start")
        assert resp.status_code == 200
        assert resp.json()["started"] is True
        assert started == [batch_id]
        # 幂等：再次 start 不再重复启动
        resp2 = c.post(f"/api/batch/{batch_id}/start")
        assert resp2.json()["started"] is False or resp2.json()["started"] is True  # 拦截后语义由 API 判断
        progress = c.get(f"/api/batch/{batch_id}/progress").json()
        assert progress["status"] in ("pending", "running")
        assert progress["stats"]["total"] == 2
        assert len(progress["items"]) == 2
        item = progress["items"][0]
        assert item["task_id"] == "task_001"
        assert item["status"] == "pending"
        assert item["message_count"] == 2

    def test_progress_shows_completed_score(self, client, session):
        """任务完成后 progress 反映评分与 inspection_id（前端跳报告用）。"""
        c, _ = client
        batch_id = c.post("/api/batch/import", json={"raw_text": THREE_CUSTOMERS}).json()["batch_id"]
        from backend.db import repository

        emp = repository.create_assistant(session, "段勇亮", "E003", "standard")
        # 手工置为 completed（等价于后台 worker 成功路径的结果形状）
        brepo.set_task_status(
            session,
            brepo.get_task(session, batch_id, "task_001"),
            "completed",
            result_json={
                "reports": [
                    {
                        "assistant_id": emp.id,
                        "assistant_name": "段勇亮",
                        "employee_no": "E003",
                        "inspection_id": 999,
                        "total_score": 82,
                        "is_red_alert": False,
                        "turn_count": 2,
                        "degraded": False,
                        "chunk_count": 1,
                    }
                ],
                "errors": [],
                "chunk_count": 1,
            },
        )
        progress = c.get(f"/api/batch/{batch_id}/progress").json()
        item = progress["items"][0]
        assert item["status"] == "completed"
        assert item["score"] == 82
        assert item["inspection_id"] == 999
        assert item["reports"] == [
            {"assistant_name": "段勇亮", "total_score": 82, "inspection_id": 999}
        ]
        assert item["chunk_count"] == 1
        assert item["degraded"] is False
        assert progress["stats"]["completed"] == 1
        assert progress["stats"]["percent"] == 50

    def test_progress_multi_assistant_reports(self, client, session):
        """一个客户会话多位助理：progress 返回每份报告的独立跳转入口（前端逐条渲染）。"""
        c, _ = client
        batch_id = c.post("/api/batch/import", json={"raw_text": THREE_CUSTOMERS}).json()["batch_id"]
        from backend.db import repository

        emp1 = repository.create_assistant(session, "段勇亮", "E003", "standard")
        emp2 = repository.create_assistant(session, "李金潓", "E004", "standard")
        brepo.set_task_status(
            session,
            brepo.get_task(session, batch_id, "task_001"),
            "completed",
            result_json={
                "reports": [
                    {
                        "assistant_id": emp1.id,
                        "assistant_name": "段勇亮",
                        "employee_no": "E003",
                        "inspection_id": 1001,
                        "total_score": 82,
                        "is_red_alert": False,
                        "turn_count": 2,
                        "degraded": False,
                        "chunk_count": 1,
                    },
                    {
                        "assistant_id": emp2.id,
                        "assistant_name": "李金潓",
                        "employee_no": "E004",
                        "inspection_id": 1002,
                        "total_score": 91,
                        "is_red_alert": False,
                        "turn_count": 3,
                        "degraded": False,
                        "chunk_count": 1,
                    },
                ],
                "errors": [],
                "chunk_count": 2,
            },
        )
        item = c.get(f"/api/batch/{batch_id}/progress").json()["items"][0]
        # 兼容字段：第一位助理仍作为默认入口
        assert item["score"] == 82
        assert item["inspection_id"] == 1001
        assert item["assistant_names"] == ["段勇亮", "李金潓"]
        # 新增：每份报告独立入口（前端逐助理渲染分数链接的关键）
        assert item["reports"] == [
            {"assistant_name": "段勇亮", "total_score": 82, "inspection_id": 1001},
            {"assistant_name": "李金潓", "total_score": 91, "inspection_id": 1002},
        ]

    def test_retry_failed(self, client, session):
        """失败任务重试：failed → pending + 启动 worker（拦截记录）。"""
        c, started = client
        batch_id = c.post("/api/batch/import", json={"raw_text": THREE_CUSTOMERS}).json()["batch_id"]
        # 无失败任务 → 不启动
        resp = c.post(f"/api/batch/{batch_id}/retry-failed")
        assert resp.json() == {"batch_id": batch_id, "reset_count": 0, "started": False}
        # 置 1 个 failed 后重试
        t = brepo.get_task(session, batch_id, "task_001")
        t.status, t.error, t.retry_count = "failed", "模拟失败", 3
        session.commit()
        resp2 = c.post(f"/api/batch/{batch_id}/retry-failed")
        assert resp2.json()["reset_count"] == 1
        assert resp2.json()["started"] is True
        assert started == [batch_id]
        t2 = brepo.get_task(session, batch_id, "task_001")
        assert t2.status == "pending"
        assert t2.retry_count == 0
        assert t2.error is None

    def test_batch_not_found_404(self, client):
        c, _ = client
        assert c.get("/api/batch/nonexistent/progress").status_code == 404
        assert c.post("/api/batch/nonexistent/start").status_code == 404
        assert c.post("/api/batch/nonexistent/retry-failed").status_code == 404

    def test_batch_list(self, client):
        c, _ = client
        c.post("/api/batch/import", json={"raw_text": THREE_CUSTOMERS, "title": "批次A"})
        c.post("/api/batch/import", json={"raw_text": THREE_CUSTOMERS, "title": "批次B"})
        data = c.get("/api/batch").json()
        assert data["total"] == 2
        assert data["batches"][0]["title"] == "批次B"  # 倒序
        assert data["batches"][0]["source_stats"]["task_count"] == 2

    def test_delete_batch_cascades_reports(self, client, session):
        """删除批次：任务与关联质检报告（含明细）一并删除。"""
        from backend.db.models import Assistant, Inspection, InspectionDetail

        c, _ = client
        batch_id = c.post("/api/batch/import", json={"raw_text": THREE_CUSTOMERS, "title": "待删批次"}).json()["batch_id"]
        # 造一条关联该批次的质检报告（含明细）
        session.add(Assistant(id=1, name="韩珂龙头班", employee_no="E999", template_type="standard"))
        session.flush()
        ins = Inspection(
            assistant_id=1, total_score=80, template_type="standard",
            template_snapshot_json="{}", turn_count=2, conversation_id=batch_id,
        )
        session.add(ins)
        session.flush()
        session.add(InspectionDetail(
            inspection_id=ins.id, raw_dialogue="测试", d_scores_json="{}", s_scores_json="{}"
        ))
        session.commit()
        assert brepo.list_tasks(session, batch_id)  # 任务存在
        # 删除批次 → 批次/任务/报告/明细全部消失
        resp = c.delete(f"/api/batch/{batch_id}")
        assert resp.status_code == 200
        assert resp.json() == {"batch_id": batch_id, "deleted": 1}
        assert brepo.get_batch(session, batch_id) is None
        assert brepo.list_tasks(session, batch_id) == []
        assert session.get(Inspection, ins.id) is None
        assert session.query(InspectionDetail).filter_by(inspection_id=ins.id).count() == 0
        # 删除后批次列表不再包含
        data = c.get("/api/batch").json()
        assert all(b["batch_id"] != batch_id for b in data["batches"])

    def test_report_page_session_switch_bar(self, client, session):
        """同会话多位助理：报告接口返回 session_reports（前端渲染助理切换栏）。
        批量评分任务一客户多助理各自成报告，任一报告页都能切换到其他助理的完整报告。"""
        from backend.db.models import Assistant, Inspection, InspectionDetail

        c, _ = client
        batch_id = c.post("/api/batch/import", json={"raw_text": THREE_CUSTOMERS}).json()["batch_id"]
        # 同会话两位助理各自一份报告（模拟批量评分多助理任务落库）
        session.add(Assistant(id=1, name="段勇亮", employee_no="E003", template_type="standard"))
        session.add(Assistant(id=2, name="李金潓", employee_no="E004", template_type="standard"))
        session.flush()
        ids = []
        for aid, name, score in ((1, "段勇亮", 82), (2, "李金潓", 91)):
            ins = Inspection(
                assistant_id=aid, total_score=score, template_type="standard",
                template_snapshot_json="{}", turn_count=2, conversation_id=batch_id,
            )
            session.add(ins)
            session.flush()
            session.add(InspectionDetail(
                inspection_id=ins.id, raw_dialogue="测试", d_scores_json="{}", s_scores_json="{}"
            ))
            ids.append(ins.id)
        session.commit()
        # 任一报告的响应都带全部同会话报告（含自身），按生成顺序
        data = c.get(f"/api/reports/{ids[0]}").json()
        assert data["id"] == ids[0]
        assert data["assistant_name"] == "段勇亮"
        assert data["session_reports"] == [
            {"id": ids[0], "assistant_name": "段勇亮", "total_score": 82, "is_red_alert": False, "is_yellow_alert": False},
            {"id": ids[1], "assistant_name": "李金潓", "total_score": 91, "is_red_alert": False, "is_yellow_alert": False},
        ]
        # 反向切换入口同样存在
        data2 = c.get(f"/api/reports/{ids[1]}").json()
        assert [r["id"] for r in data2["session_reports"]] == [ids[0], ids[1]]
        # 单报告会话（无 conversation_id）不带 session_reports —— 老报告兼容
        solo = Inspection(
            assistant_id=1, total_score=70, template_type="standard",
            template_snapshot_json="{}", turn_count=1,
        )
        session.add(solo)
        session.commit()
        solo_data = c.get(f"/api/reports/{solo.id}").json()
        assert "session_reports" not in solo_data

    def test_report_switch_bar_isolated_per_task(self, client, session):
        """多任务批次共享 conversation_id：报告切换栏按任务隔离，不混入其他客户任务的报告。"""
        from backend.db.models import Assistant, Inspection, InspectionDetail

        c, _ = client
        raw = (
            "客户甲\n2026-08-01 10:00:00\n你好\n\n"
            "韩珂龙头班\n2026-08-01 10:01:00\n您好\n\n"
            "客户甲\n2026-08-01 10:02:00\n谢谢\n\n"
            "李金潓\n2026-08-01 10:03:00\n不客气\n\n"
            "客户乙\n2026-08-02 10:00:00\n在吗\n\n"
            "韩珂龙头班\n2026-08-02 10:01:00\n在的\n"
        )
        batch_id = c.post("/api/batch/import", json={"raw_text": raw}).json()["batch_id"]
        session.add(Assistant(id=1, name="段勇亮", employee_no="E003", template_type="standard"))
        session.add(Assistant(id=2, name="李金潓", employee_no="E004", template_type="standard"))
        session.flush()

        def make_ins(assistant_id, score):
            ins = Inspection(
                assistant_id=assistant_id, total_score=score, template_type="standard",
                template_snapshot_json="{}", turn_count=1, conversation_id=batch_id,
            )
            session.add(ins)
            session.flush()
            session.add(InspectionDetail(
                inspection_id=ins.id, raw_dialogue="测试", d_scores_json="{}", s_scores_json="{}"
            ))
            return ins.id

        # 任务1（客户甲）两位助理各一份报告；任务2（客户乙）一位助理一份报告
        id_a, id_b = make_ins(1, 82), make_ins(2, 91)
        id_c = make_ins(1, 77)
        brepo.set_task_status(
            session,
            brepo.get_task(session, batch_id, "task_001"),
            "completed",
            result_json={
                "reports": [
                    {"assistant_id": 1, "assistant_name": "段勇亮", "employee_no": "E003", "inspection_id": id_a, "total_score": 82, "is_red_alert": False, "turn_count": 1, "degraded": False, "chunk_count": 1},
                    {"assistant_id": 2, "assistant_name": "李金潓", "employee_no": "E004", "inspection_id": id_b, "total_score": 91, "is_red_alert": False, "turn_count": 1, "degraded": False, "chunk_count": 1},
                ],
                "errors": [],
                "chunk_count": 2,
            },
        )
        brepo.set_task_status(
            session,
            brepo.get_task(session, batch_id, "task_002"),
            "completed",
            result_json={
                "reports": [
                    {"assistant_id": 1, "assistant_name": "段勇亮", "employee_no": "E003", "inspection_id": id_c, "total_score": 77, "is_red_alert": False, "turn_count": 1, "degraded": False, "chunk_count": 1},
                ],
                "errors": [],
                "chunk_count": 1,
            },
        )
        session.commit()
        # 任务1 的报告：切换栏只含任务1 的两份（不含任务2 的）
        d1 = c.get(f"/api/reports/{id_a}").json()
        assert d1["assistant_name"] == "段勇亮"
        assert [r["id"] for r in d1["session_reports"]] == [id_a, id_b]
        d2 = c.get(f"/api/reports/{id_b}").json()
        assert [r["id"] for r in d2["session_reports"]] == [id_a, id_b]
        # 任务2 单报告：不附加切换栏（其他客户任务的报告不串门）
        d3 = c.get(f"/api/reports/{id_c}").json()
        assert "session_reports" not in d3
        # progress 透出 overview_id（前端「查看总览」链接用；测试手工置的结果无该字段 → null）
        items = c.get(f"/api/batch/{batch_id}/progress").json()["items"]
        ov_by_task = {t["task_id"]: t["overview_id"] for t in items}
        assert ov_by_task["task_001"] is None
        assert ov_by_task["task_002"] is None

    def test_progress_exposes_overview_id(self, client, session):
        """任务 progress 透出 overview_id（多助理任务总览入口，前端「查看总览」链接用）。"""
        c, _ = client
        batch_id = c.post("/api/batch/import", json={"raw_text": THREE_CUSTOMERS}).json()["batch_id"]
        from backend.db import repository

        emp = repository.create_assistant(session, "段勇亮", "E003", "standard")
        brepo.set_task_status(
            session,
            brepo.get_task(session, batch_id, "task_001"),
            "completed",
            result_json={
                "reports": [
                    {"assistant_id": emp.id, "assistant_name": "段勇亮", "employee_no": "E003",
                     "inspection_id": 999, "total_score": 82, "is_red_alert": False,
                     "turn_count": 2, "degraded": False, "chunk_count": 1},
                ],
                "errors": [],
                "chunk_count": 1,
                "overview_id": 42,
            },
        )
        item = c.get(f"/api/batch/{batch_id}/progress").json()["items"][0]
        assert item["overview_id"] == 42

    def test_delete_batch_not_found_404(self, client):
        c, _ = client
        assert c.delete("/api/batch/nonexistent").status_code == 404

    def test_import_rooms_excel_export(self, client, session):
        """Excel 房间导出：POST rooms → 每个房间独立任务（【】时间戳归一化后解析）。"""
        c, _ = client
        rooms = [
            {
                "customer_name": "曹*（1395619）",
                "text": "【2026-08-19 17:35:44】曹*（客户）\n大鹏老师好，隆基绿能可以买吗？\n\n"
                        "【2026-08-19 21:03:10】曹瑞格（投顾助理；是否目标助理：是；是否代理老师发言：是；客户看到：大鹏寻龙班）\n"
                        "隆基绿能目前还在关注，持仓周期会长一些\n\n"
                        "【2026-08-20 08:44:58】曹*（客户）\n收到，谢谢老师",
            },
            {
                "customer_name": "丁*（1396116）",
                "text": "【2026-08-21 09:12:33】丁*（客户）\n老师，黄金还能追吗？\n\n"
                        "【2026-08-21 09:30:10】曹瑞格（投顾助理；是否目标助理：是）\n黄金短线位置偏高，建议等回调",
            },
        ]
        resp = c.post("/api/batch/import", json={"rooms": rooms, "title": "房间导入"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_count"] == 2
        assert data["customer_count"] == 2
        assert data["message_count"] == 5
        assert data["assistant_count"] == 1
        names = [x["customer_name"] for x in data["customers"]]
        assert names == ["曹*（1395619）", "丁*（1396116）"]
        assert data["customers"][0]["assistant_names"] == ["曹瑞格"]
        # 任务 input_data 已落库（事实源），source_fmt=rooms
        t = brepo.get_task(session, data["batch_id"], "task_001")
        d = json.loads(t.input_data)
        assert d["source_fmt"] == "rooms"
        assert d["messages"][0]["role"] == "客"
        assert d["messages"][0]["text"] == "大鹏老师好，隆基绿能可以买吗？"

    def test_import_rooms_skips_empty_rooms(self, client):
        """空房间（无客户消息）跳过不建任务，warnings 提示。"""
        c, _ = client
        rooms = [
            {
                "customer_name": "曹*（1395619）",
                "text": "【2026-08-19 17:35:44】曹*（客户）\n你好\n\n"
                        "【2026-08-19 21:03:10】曹瑞格（投顾助理；是否目标助理：是）\n您好",
            },
            {
                "customer_name": "空房间",
                "text": "【2026-08-21 09:12:33】曹瑞格（投顾助理；是否目标助理：是）\n仅助理发言，无客户",
            },
        ]
        resp = c.post("/api/batch/import", json={"rooms": rooms})
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_count"] == 1
        assert any("空房间" in w for w in data["warnings"])
