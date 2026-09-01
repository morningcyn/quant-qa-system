# 批量评分管理：状态机 / 重试 / 员工匹配 / 断点续跑 / 汇总降级
import json
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config import DEFAULT_TEMPLATES
from backend.db import batch_repository as brepo
from backend.db import repository
from backend.db.models import Base
from backend.services import multiparser
from backend.services.batch import aggregator, chunker
from backend.services.batch import manager as mgr_mod
from backend.services.batch.manager import mgr
from backend.services.batch.splitter import message_to_dict, split_customers
from backend.services.llm.base import LLMError
from backend.services.multiparser import Segment
from backend.services.parser import Turn

# 复用 conftest 的 Mock client 与合法输出
from tests.conftest import MockLLMByUserClient, MockLLMClient, valid_llm_json

THREE_LINE = (
    "邯郸赢家0878\n2026-07-03 13:12:42\n你好韩老师！300166提醒加仓没看到现在能加吗？\n\n"
    "韩珂龙头班\n2026-07-03 14:32:24\n可以按照中线模式低吸加仓5%，不要追涨就好\n\n"
    "邯郸赢家0878\n2026-07-10 13:31:50\n韩老师好！000420现在能加仓吗？"
)


@pytest.fixture()
def env(monkeypatch):
    """独立内存库（模板 + 空员工表）+ 让 manager 使用它 + 退避归零。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as s:
        for ttype, config in DEFAULT_TEMPLATES.items():
            repository.upsert_template(s, ttype, config["name"], config)
    monkeypatch.setattr(mgr_mod, "SessionLocal", factory)
    monkeypatch.setattr(mgr_mod, "RETRY_DELAYS", [0, 0, 0])
    return factory


def mock_runtime(monkeypatch, client):
    monkeypatch.setattr(mgr_mod.factory, "get_active_runtime", lambda session: (client, {"temperature": 0.2}))


def make_batch(factory, raw_text, employees=(), name_map=None):
    """建批次 + 任务（按切分结果），返回 batch_id。employees: 预先创建的员工列表 [(name, no)]。"""
    with factory() as s:
        for name, no in employees:
            repository.create_assistant(s, name, no, "standard")
        split = split_customers(raw_text, repository.list_assistants(s), name_map)
        batch_id = "b_test"
        brepo.create_batch_run(
            s, batch_id, "测试批次",
            {"customer_count": len(split["customers"]), "task_count": split["task_count"]},
            split["warnings"],
        )
        brepo.create_tasks(
            s,
            batch_id,
            [
                {
                    "task_id": f"task_{i + 1:03d}",
                    "customer_id": c.customer_id,
                    "customer_name": c.customer_name,
                    "assistant_ids": [],
                    "input_data": {
                        "messages": [message_to_dict(m) for m in c.messages],
                        "title": "测试批次",
                        "source_fmt": "text",
                    },
                }
                for i, c in enumerate(split["customers"])
            ],
        )
        return batch_id, split["task_count"]


def task_status(factory, batch_id, task_id="task_001"):
    with factory() as s:
        t = brepo.get_task(s, batch_id, task_id)
        return t.status, t.retry_count, t.error, t.result_json


class TestBatchTaskManager:
    def test_task_execution_respects_global_concurrency_limit(self, monkeypatch):
        active = 0
        max_active = 0
        tasks = {
            ("b_limit", f"task_{i:03d}"): SimpleNamespace(retry_count=0)
            for i in range(6)
        }

        class FakeSession:
            def close(self):
                pass

        async def fake_execute(session, task):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {}

        monkeypatch.setattr(mgr_mod, "SessionLocal", FakeSession)
        monkeypatch.setattr(
            mgr_mod.brepo,
            "get_task",
            lambda session, batch_id, task_id: tasks[(batch_id, task_id)],
        )
        monkeypatch.setattr(mgr_mod.brepo, "set_task_status", lambda *args, **kwargs: None)
        monkeypatch.setattr(mgr, "_execute", fake_execute)

        async def run_tasks():
            await asyncio.gather(
                *(
                    mgr._run_task(SimpleNamespace(batch_id="b_limit", task_id=f"task_{i:03d}"))
                    for i in range(6)
                )
            )

        asyncio.run(run_tasks())

        assert max_active == mgr.MAX_CONCURRENT_TASKS

    def test_task_execution_closes_owned_llm_client(self, env, monkeypatch):
        class ClosingClient(MockLLMClient):
            def __init__(self):
                super().__init__([valid_llm_json()])
                self.closed = False

            async def aclose(self):
                self.closed = True

        client = ClosingClient()
        batch_id, _ = make_batch(
            env,
            THREE_LINE,
            employees=[("段勇亮", "E003")],
            name_map={"韩珂龙头班": "段勇亮"},
        )
        mock_runtime(monkeypatch, client)
        with env() as s:
            task = brepo.list_tasks(s, batch_id)[0]

        asyncio.run(mgr._run_task(task))

        assert client.closed is True

    def test_happy_path_completed(self, env, monkeypatch):
        """happy path：任务 completed、报告落库、conversation_id=batch_id。"""
        factory = env
        batch_id, _ = make_batch(factory, THREE_LINE, employees=[("段勇亮", "E003")], name_map={"韩珂龙头班": "段勇亮"})
        mock_runtime(monkeypatch, MockLLMClient([valid_llm_json()]))
        with factory() as s:
            task = brepo.list_tasks(s, batch_id)[0]
        import asyncio

        asyncio.run(mgr._run_task(task))
        status, retry, error, result_json = task_status(factory, batch_id)
        assert status == "completed"
        assert retry == 0 and error is None
        result = json.loads(result_json)
        assert len(result["reports"]) == 1
        report = result["reports"][0]
        assert report["assistant_name"] == "段勇亮"
        assert report["chunk_count"] == 1
        assert report["degraded"] is False
        # 单助理任务不生成总览（总览用于多助理评分对比）
        assert result["overview_id"] is None
        with factory() as s:
            ins = repository.get_inspection(s, report["inspection_id"])
            assert ins is not None
            assert ins.conversation_id == batch_id
            assert ins.total_score == 69
            assert ins.evaluatee == "段勇亮"

    def test_multi_assistant_generates_overview(self, env, monkeypatch):
        """多助理会话：评分后自动生成本次客户服务总览（LLM 汇总，落库供总览页查看）。"""
        from tests.conftest import MockLLMByUserClient, overview_llm_json

        factory = env
        raw = (
            "客户甲\n2026-08-01 10:00:00\n你好韩老师！\n\n"
            "韩珂龙头班\n2026-08-01 10:01:00\n您好，帮您看看\n\n"
            "客户甲\n2026-08-01 10:02:00\n谢谢\n\n"
            "李金潓\n2026-08-01 10:03:00\n不客气，有情况随时联系\n"
        )
        batch_id, _ = make_batch(
            factory,
            raw,
            employees=[("段勇亮", "E003"), ("李金潓", "E004")],
            name_map={"韩珂龙头班": "段勇亮"},
        )
        # 路由：总览调用（user 含「请按系统提示输出总览 JSON」）优先；评分调用按「员工姓名」子串
        routes = [
            ("请按系统提示输出总览 JSON", overview_llm_json()),
            ("员工姓名", valid_llm_json()),
        ]
        mock_runtime(monkeypatch, MockLLMByUserClient(routes))
        with factory() as s:
            task = brepo.list_tasks(s, batch_id)[0]
        import asyncio

        asyncio.run(mgr._run_task(task))
        status, retry, error, result_json = task_status(factory, batch_id)
        assert status == "completed" and retry == 0 and error is None
        result = json.loads(result_json)
        assert len(result["reports"]) == 2
        assert result["overview_id"]
        # 总览落库：participants 聚合两位助理；conversation_id 用「batch_id:task_id」每任务唯一锚点
        # （service_overviews.conversation_id 有 UNIQUE 约束，批次内多任务共享 batch_id 会冲突）
        with factory() as s:
            ov = repository.get_overview(s, result["overview_id"])
            assert ov is not None
            assert ov.conversation_id == f"{batch_id}:task_001"
            data = json.loads(ov.summary_json)
            names = sorted(p["name"] for p in data["participants"])
            assert names == ["李金潓", "段勇亮"]

    def test_multi_task_overviews_do_not_conflict(self, env, monkeypatch):
        """批次内两个多助理任务先后生成总览：conversation_id 每任务唯一，第二个不再 UNIQUE 冲突。"""
        from tests.conftest import MockLLMByUserClient, overview_llm_json

        factory = env
        raw = (
            "客户甲\n2026-08-01 10:00:00\n你好韩老师！\n\n"
            "韩珂龙头班\n2026-08-01 10:01:00\n您好，帮您看看\n\n"
            "客户甲\n2026-08-01 10:02:00\n谢谢\n\n"
            "李金潓\n2026-08-01 10:03:00\n不客气，有情况随时联系\n\n"
            "客户乙\n2026-08-02 10:00:00\n李老师您好\n\n"
            "李金潓\n2026-08-02 10:01:00\n您好\n\n"
            "客户乙\n2026-08-02 10:02:00\n辛苦了\n\n"
            "韩珂龙头班\n2026-08-02 10:03:00\n不客气\n"
        )
        batch_id, _ = make_batch(
            factory,
            raw,
            employees=[("段勇亮", "E003"), ("李金潓", "E004")],
            name_map={"韩珂龙头班": "段勇亮"},
        )
        routes = [
            ("请按系统提示输出总览 JSON", overview_llm_json()),
            ("员工姓名", valid_llm_json()),
        ]
        mock_runtime(monkeypatch, MockLLMByUserClient(routes))
        import asyncio

        with factory() as s:
            tasks = brepo.list_tasks(s, batch_id)
        # 顺序执行两个多助理任务：第一个总览落库后，第二个必须仍能生成自己的总览
        for task in tasks:
            asyncio.run(mgr._run_task(task))
        with factory() as s:
            for task in tasks:
                t = brepo.get_task(s, batch_id, task.task_id)
                assert t.status == "completed"
                result = json.loads(t.result_json)
                assert result["overview_id"]
                ov = repository.get_overview(s, result["overview_id"])
                assert ov is not None
            conv_ids = [
                repository.get_overview(s, json.loads(brepo.get_task(s, batch_id, t.task_id).result_json)["overview_id"]).conversation_id
                for t in tasks
            ]
        assert len(set(conv_ids)) == 2  # 每任务唯一，互不冲突
        assert all(c.startswith(batch_id + ":") for c in conv_ids)

    def test_overview_save_failure_still_completes(self, env, monkeypatch):
        """总览落库失败（UNIQUE 冲突等）：session 回滚后任务仍 completed，报告不受影响。"""
        from tests.conftest import MockLLMByUserClient, overview_llm_json

        factory = env
        raw = (
            "客户甲\n2026-08-01 10:00:00\n你好韩老师！\n\n"
            "韩珂龙头班\n2026-08-01 10:01:00\n您好，帮您看看\n\n"
            "客户甲\n2026-08-01 10:02:00\n谢谢\n\n"
            "李金潓\n2026-08-01 10:03:00\n不客气，有情况随时联系\n"
        )
        batch_id, _ = make_batch(
            factory,
            raw,
            employees=[("段勇亮", "E003"), ("李金潓", "E004")],
            name_map={"韩珂龙头班": "段勇亮"},
        )
        routes = [
            ("请按系统提示输出总览 JSON", overview_llm_json()),
            ("员工姓名", valid_llm_json()),
        ]
        mock_runtime(monkeypatch, MockLLMByUserClient(routes))

        import sqlite3

        # 模拟真实事故：save_overview 落库抛 UNIQUE 冲突（conversation_id 已被占用）
        def boom(*a, **k):
            raise sqlite3.IntegrityError("UNIQUE constraint failed: service_overviews.conversation_id")

        monkeypatch.setattr("backend.services.dispatcher.repository.save_overview", boom)
        with factory() as s:
            task = brepo.list_tasks(s, batch_id)[0]
        import asyncio

        asyncio.run(mgr._run_task(task))
        status, retry, error, result_json = task_status(factory, batch_id)
        assert status == "completed" and retry == 0 and error is None
        result = json.loads(result_json)
        assert result["overview_id"] is None  # 总览失败不阻塞任务
        assert len(result["reports"]) == 2  # 报告照常落库

    def test_overview_failure_does_not_fail_task(self, env, monkeypatch):
        """总览 LLM 失败：规则降级总览仍落库（dispatcher 内部处理），任务照常 completed。"""
        from tests.conftest import MockLLMByUserClient

        factory = env
        raw = (
            "客户甲\n2026-08-01 10:00:00\n你好韩老师！\n\n"
            "韩珂龙头班\n2026-08-01 10:01:00\n您好，帮您看看\n\n"
            "客户甲\n2026-08-01 10:02:00\n谢谢\n\n"
            "李金潓\n2026-08-01 10:03:00\n不客气，有情况随时联系\n"
        )
        batch_id, _ = make_batch(
            factory,
            raw,
            employees=[("段勇亮", "E003"), ("李金潓", "E004")],
            name_map={"韩珂龙头班": "段勇亮"},
        )
        # 总览调用直接抛异常 → generate_overview 内部 try 已捕获并规则降级（degraded 总览）
        routes = [
            ("请按系统提示输出总览 JSON", LLMError("network", "mock 总览网络错误")),
            ("员工姓名", valid_llm_json()),
        ]
        mock_runtime(monkeypatch, MockLLMByUserClient(routes))
        with factory() as s:
            task = brepo.list_tasks(s, batch_id)[0]
        import asyncio

        asyncio.run(mgr._run_task(task))
        status, retry, error, result_json = task_status(factory, batch_id)
        assert status == "completed"
        result = json.loads(result_json)
        assert len(result["reports"]) == 2
        assert result["overview_id"]  # 规则降级总览仍生成
        with factory() as s:
            ov = repository.get_overview(s, result["overview_id"])
            assert ov.degraded is True

    def test_one_failure_does_not_block_others(self, env, monkeypatch):
        """单任务失败不阻塞：3 任务中 1 个 3 次失败 → failed，其余 completed。"""
        factory = env
        raw = (
            "客户甲\n2026-08-01 10:00:00\n你好\n\n"
            "韩珂龙头班\n2026-08-01 10:01:00\n您好\n\n"
            "客户乙\n2026-08-02 10:00:00\n在吗\n\n"
            "韩珂龙头班\n2026-08-02 10:01:00\n在的\n\n"
            "客户丙\n2026-08-03 10:00:00\n帮我看看\n\n"
            "韩珂龙头班\n2026-08-03 10:01:00\n好的\n"
        )
        batch_id, _ = make_batch(factory, raw, employees=[("段勇亮", "E003")], name_map={"韩珂龙头班": "段勇亮"})
        # 响应队列：任务1 3 次 timeout；任务2/3 各 = 评分 1 次 + 情绪 1 次
        responses = (
            [LLMError("timeout", "mock 超时")] * 3
            + [valid_llm_json(), emotion_items_json([1, 3, 5])]
            + [valid_llm_json(), emotion_items_json([1, 3, 5])]
        )
        mock_runtime(monkeypatch, MockLLMClient(responses))
        with factory() as s:
            tasks = brepo.list_tasks(s, batch_id)
        import asyncio

        async def run_all():
            for t in tasks:
                await mgr._run_task(t)

        asyncio.run(run_all())
        st1, retry1, err1, _ = task_status(factory, batch_id, "task_001")
        st2, _, _, _ = task_status(factory, batch_id, "task_002")
        st3, _, _, _ = task_status(factory, batch_id, "task_003")
        assert st1 == "failed" and retry1 == 3
        assert st2 == "completed" and st3 == "completed"

    def test_retry_then_success_retry_count_one(self, env, monkeypatch):
        """第 1 次超时 → retrying；第 2 次成功 → completed retry_count==1。"""
        factory = env
        batch_id, _ = make_batch(factory, THREE_LINE, employees=[("段勇亮", "E003")], name_map={"韩珂龙头班": "段勇亮"})
        mock_runtime(monkeypatch, MockLLMClient([LLMError("timeout", "第一次超时"), valid_llm_json()]))
        with factory() as s:
            task = brepo.list_tasks(s, batch_id)[0]
        import asyncio

        asyncio.run(mgr._run_task(task))
        status, retry, error, _ = task_status(factory, batch_id)
        assert status == "completed"
        assert retry == 1

    def test_auth_failure_direct_failed(self, env, monkeypatch):
        """致命错误（auth）→ 不重试直接 failed。"""
        factory = env
        batch_id, _ = make_batch(factory, THREE_LINE, employees=[("段勇亮", "E003")], name_map={"韩珂龙头班": "段勇亮"})
        mock_runtime(monkeypatch, MockLLMClient([LLMError("auth", "API Key 无效")]))
        with factory() as s:
            task = brepo.list_tasks(s, batch_id)[0]
        import asyncio

        asyncio.run(mgr._run_task(task))
        status, retry, error, _ = task_status(factory, batch_id)
        assert status == "failed"
        assert retry == 0
        assert "API Key" in (error or "")

    def test_auth_failure_stops_unfinished_batch(self, env, monkeypatch):
        """批次级鉴权失败：当前任务及其余未完成任务一次性 failed，不重复请求。"""
        factory = env
        raw = (
            "客户甲\n2026-08-01 10:00:00\n你好\n\n"
            "韩珂龙头班\n2026-08-01 10:01:00\n您好\n\n"
            "客户乙\n2026-08-02 10:00:00\n在吗\n\n"
            "韩珂龙头班\n2026-08-02 10:01:00\n在的\n\n"
            "客户丙\n2026-08-03 10:00:00\n帮我看看\n\n"
            "韩珂龙头班\n2026-08-03 10:01:00\n好的\n"
        )
        batch_id, _ = make_batch(factory, raw, employees=[("段勇亮", "E003")], name_map={"韩珂龙头班": "段勇亮"})
        client = MockLLMClient([LLMError("auth", "API Key 无效")])
        mock_runtime(monkeypatch, client)
        with factory() as s:
            task = brepo.list_tasks(s, batch_id)[0]
        import asyncio

        asyncio.run(mgr._run_task(task))
        with factory() as s:
            stats = brepo.task_stats(s, batch_id)
            tasks = brepo.list_tasks(s, batch_id)
        assert len(client.calls) == 1
        assert stats["failed"] == 3
        assert stats["pending"] == stats["processing"] == stats["retrying"] == 0
        assert all(t.status == "failed" and "API Key" in (t.error or "") for t in tasks)

    def test_unmapped_assistant_failed_with_guide(self, env, monkeypatch):
        """未匹配员工 → failed 并提示去员工档案创建（全自动匹配语义）。"""
        factory = env
        batch_id, _ = make_batch(factory, THREE_LINE)  # 无员工、无 name_map → 韩珂龙头班 未匹配
        mock_runtime(monkeypatch, MockLLMClient([valid_llm_json()]))  # 不应被调用
        with factory() as s:
            task = brepo.list_tasks(s, batch_id)[0]
        import asyncio

        asyncio.run(mgr._run_task(task))
        status, retry, error, _ = task_status(factory, batch_id)
        assert status == "failed"
        assert retry == 0
        assert "未匹配到员工档案" in (error or "")
        assert "韩珂龙头班" in (error or "")

    def test_reset_failed_reruns(self, env, monkeypatch):
        """重新评分失败任务：failed → pending（retry_count 归零）→ 重跑成功。"""
        factory = env
        batch_id, _ = make_batch(factory, THREE_LINE, employees=[("段勇亮", "E003")], name_map={"韩珂龙头班": "段勇亮"})
        # 先失败：错误 key（auth 致命）
        mock_runtime(monkeypatch, MockLLMClient([LLMError("auth", "bad key")]))
        with factory() as s:
            task = brepo.list_tasks(s, batch_id)[0]
        import asyncio

        asyncio.run(mgr._run_task(task))
        status, _, _, _ = task_status(factory, batch_id)
        assert status == "failed"
        # 修复后重跑（reset_failed_tasks 等价于 retry-failed API 内部逻辑）
        with factory() as s:
            assert brepo.reset_failed_tasks(s, batch_id) == 1
            task = brepo.list_tasks(s, batch_id)[0]
        mock_runtime(monkeypatch, MockLLMClient([valid_llm_json()]))
        asyncio.run(mgr._run_task(task))
        status, retry, error, _ = task_status(factory, batch_id)
        assert status == "completed" and retry == 0

    def test_stale_processing_reset(self, env):
        """断点续跑：处理中超时（10min）→ 置回 pending；新任务不受影响。"""
        factory = env
        batch_id, _ = make_batch(factory, THREE_LINE, employees=[("段勇亮", "E003")], name_map={"韩珂龙头班": "段勇亮"})
        from datetime import datetime, timedelta

        with factory() as s:
            t = brepo.get_task(s, batch_id, "task_001")
            t.status = "processing"
            t.updated_at = datetime.now() - timedelta(minutes=11)
            s.commit()
            assert brepo.reset_stale_processing(s, older_than=timedelta(minutes=10)) == 1
        status, _, _, _ = task_status(factory, batch_id)
        assert status == "pending"

    def test_worker_finishes_batch_done(self, env, monkeypatch):
        """完整 worker 循环：认领全部任务后批次置 done。"""
        factory = env
        batch_id, _ = make_batch(factory, THREE_LINE, employees=[("段勇亮", "E003")], name_map={"韩珂龙头班": "段勇亮"})
        mock_runtime(monkeypatch, MockLLMClient([valid_llm_json()]))
        import asyncio

        asyncio.run(mgr._worker(batch_id))
        with factory() as s:
            run = brepo.get_batch(s, batch_id)
            assert run.status == "done"
            assert brepo.is_batch_finished(s, batch_id)

    def test_worker_resumes_stale_after_fast_restart(self, env, monkeypatch):
        """强杀快速重启：processing 未满 10 分钟 → worker 空转等到 stale 阈值 → 自动重置续跑完成。"""
        from datetime import datetime, timedelta

        factory = env
        batch_id, _ = make_batch(factory, THREE_LINE, employees=[("段勇亮", "E003")], name_map={"韩珂龙头班": "段勇亮"})
        # 模拟强杀遗留：processing 且 updated_at 仅 2 分钟前（未到 10 分钟阈值）
        with factory() as s:
            t = brepo.get_task(s, batch_id, "task_001")
            t.status, t.updated_at = "processing", datetime.now() - timedelta(minutes=2)
            s.commit()
        mock_runtime(monkeypatch, MockLLMClient([valid_llm_json()]))
        # 让 stale 判定立即成立：monkeypatch reset 的阈值语义——直接前置把任务置老
        # （worker 循环内调 reset_stale_processing，我们用 2 分钟老 + 10 分钟阈值 → 空转；
        #  为不真等 8 分钟，把任务置 11 分钟老模拟「到点后」）
        import asyncio

        async def run():
            worker = asyncio.create_task(mgr._worker(batch_id))
            await asyncio.sleep(0.2)
            # 首轮空转后：任务到 stale 阈值 → 重置 → 认领 → 评分完成
            with factory() as s:
                t = brepo.get_task(s, batch_id, "task_001")
                t.updated_at = datetime.now() - timedelta(minutes=11)
                s.commit()
            await asyncio.wait_for(worker, timeout=10)

        asyncio.run(run())
        status, retry, error, result_json = task_status(factory, batch_id)
        assert status == "completed"
        with factory() as s:
            assert brepo.get_batch(s, batch_id).status == "done"


class Emp:
    def __init__(self, id, name, employee_no):
        self.id, self.name, self.employee_no = id, name, employee_no


def make_segment(n, char_len=1500):
    turns = []
    for i in range(n):
        role = "客" if (i % 2 == 0) else "助"
        turns.append(Turn(role=role, speaker="客户" if role == "客" else "助理A", text="字" * char_len, turn_no=i + 1))
    return Segment(turns=turns, context_turns=[], text="", context_text=None, evaluation_context={}, start_turn=1, end_turn=n)


class TestAggregator:
    def test_single_chunk_zero_extra_llm(self, env, monkeypatch):
        """单 chunk：零额外 LLM 调用（1 次评分即最终结果）。"""
        factory = env
        with factory() as s:
            repository.create_assistant(s, "王萌", "E001", "standard")
            assistant = repository.get_assistant_by_no(s, "E001")
        client = MockLLMClient([valid_llm_json()])
        chunks = chunker.chunk_segment(make_segment(5))
        import asyncio

        with factory() as s:
            merged, degraded = asyncio.run(
                aggregator.aggregate_chunks(s, chunks, assistant, "王萌", client, {}, None, "standard")
            )
        assert degraded is False
        assert merged.total_score == 69
        assert len(client.calls) == 1  # 仅 1 次评分调用

    def test_multi_chunk_summarized_by_llm(self, env, monkeypatch):
        """多 chunk：逐 chunk 评分 + 汇总 Agent 一次调用；最终分数/高亮合并。"""
        factory = env
        with factory() as s:
            repository.create_assistant(s, "王萌", "E001", "standard")
            assistant = repository.get_assistant_by_no(s, "E001")
        chunks = chunker.chunk_segment(make_segment(30))  # 30×1500=45000 → 多窗
        assert len(chunks) >= 2
        routes = [
            ("请汇总输出最终质检 JSON", valid_llm_json()),
            ("员工姓名", valid_llm_json()),
        ]
        client = MockLLMByUserClient(routes)
        import asyncio

        with factory() as s:
            merged, degraded = asyncio.run(
                aggregator.aggregate_chunks(s, chunks, assistant, "王萌", client, {}, None, "standard")
            )
        assert degraded is False
        assert merged.total_score == 69
        # 评分调用数 = chunk 数；汇总调用 1 次
        score_calls = sum(1 for c in client.calls if "员工姓名" in c["user"] and "请汇总输出最终质检 JSON" not in c["user"])
        sum_calls = sum(1 for c in client.calls if "请汇总输出最终质检 JSON" in c["user"])
        assert score_calls == len(chunks)
        assert sum_calls == 1

    def test_summarize_failure_fallback_degraded(self, env, monkeypatch):
        """汇总 LLM 失败 → 规则合并降级（degraded=True），结果仍可落库。"""
        factory = env
        with factory() as s:
            repository.create_assistant(s, "王萌", "E001", "standard")
            assistant = repository.get_assistant_by_no(s, "E001")
        chunks = chunker.chunk_segment(make_segment(30))
        routes = [
            ("请汇总输出最终质检 JSON", LLMError("network", "mock 网络错误")),
            ("员工姓名", valid_llm_json()),
        ]
        client = MockLLMByUserClient(routes)
        import asyncio

        with factory() as s:
            merged, degraded = asyncio.run(
                aggregator.aggregate_chunks(s, chunks, assistant, "王萌", client, {}, None, "standard")
            )
        assert degraded is True
        assert merged.total_score == 69  # 均值合并后 guardrails 重算
        assert len(merged.highlight_dialogue) >= 1  # highlight 按 turn 去重保留

    def test_fallback_merge_red_alert_any(self, env):
        """降级合并：红灯任一命中 → 最终红灯。"""
        r1 = __import__("backend.schemas.inspection", fromlist=["LLMResultSchema"]).LLMResultSchema.model_validate(
            json.loads(valid_llm_json(red=True, red_reasons=["承诺收益"]))
        )
        r2 = __import__("backend.schemas.inspection", fromlist=["LLMResultSchema"]).LLMResultSchema.model_validate(
            json.loads(valid_llm_json())
        )
        from backend.services.batch.aggregator import _fallback_merge

        merged = _fallback_merge([r1, r2])
        assert merged.is_red_alert is True
        assert "承诺收益" in merged.red_alert_reasons


def emotion_items_json(turn_nos, emotion="担忧", intensity=2, trigger="行情波动"):
    return json.dumps(
        {
            "items": [
                {"turn_no": t, "emotion": emotion, "intensity": intensity,
                 "confidence": 0.9, "trigger": trigger, "evidence": "好的"}
                for t in turn_nos
            ]
        },
        ensure_ascii=False,
    )


def customer_turn_nos(factory, batch_id):
    """从任务 input_data 取客轮 turn_no 列表（情绪 mock 输出需要精确编号）。"""
    with factory() as s:
        task = brepo.list_tasks(s, batch_id)[0]
        data = json.loads(task.input_data)
        return [m["turn_no"] for m in data["messages"] if m["role"] == "客"]


def emotion_row(factory, conversation_id):
    with factory() as s:
        return repository.get_emotion_session_by_conversation(s, conversation_id)


class TestBatchEmotion:
    """批量任务评分时自动情绪分析：情绪行存在 / 失败不影响任务状态。"""

    def test_emotion_auto_analyzed_task_completes(self, env, monkeypatch):
        """多助理任务：评分 + 总览 + 情绪分析全部完成后任务 completed，情绪行落库。"""
        from tests.conftest import MockLLMByUserClient, overview_llm_json

        factory = env
        raw = (
            "客户甲\n2026-08-01 10:00:00\n有点慌\n\n"
            "韩珂龙头班\n2026-08-01 10:01:00\n您好，帮您看看\n\n"
            "客户甲\n2026-08-01 10:02:00\n好的谢谢\n\n"
            "李金潓\n2026-08-01 10:03:00\n不客气，有情况随时联系\n"
        )
        batch_id, _ = make_batch(
            factory,
            raw,
            employees=[("段勇亮", "E003"), ("李金潓", "E004")],
            name_map={"韩珂龙头班": "段勇亮"},
        )
        turns = customer_turn_nos(factory, batch_id)
        assert turns == [1, 3]
        # 情绪输出：turn1 焦虑 / turn3 中性（末条客轮 → 当前情绪=中性）
        emo_json = json.dumps(
            {
                "items": [
                    {"turn_no": 1, "emotion": "焦虑", "intensity": 3, "confidence": 0.9, "trigger": "行情波动", "evidence": "好的"},
                    {"turn_no": 3, "emotion": "中性", "intensity": 0, "confidence": 0.9, "trigger": "未知", "evidence": "好的"},
                ]
            },
            ensure_ascii=False,
        )
        # 路由顺序：总览 → 情绪（特征词「逐条标注情绪」）→ 评分（「员工姓名」）
        routes = [
            ("请按系统提示输出总览 JSON", overview_llm_json()),
            ("请对以下客户消息逐条标注情绪", emo_json),
            ("员工姓名", valid_llm_json()),
        ]
        mock_runtime(monkeypatch, MockLLMByUserClient(routes))
        with factory() as s:
            task = brepo.list_tasks(s, batch_id)[0]
        import asyncio

        asyncio.run(mgr._run_task(task))
        status, retry, error, result_json = task_status(factory, batch_id)
        assert status == "completed" and retry == 0 and error is None
        result = json.loads(result_json)
        assert result["emotion_id"]  # 情绪行 id 回写 result_json
        assert len(result["reports"]) == 2  # 报告照常
        # 情绪行：锚点 batch_id:task_id，当前情绪=末条客轮（中性）
        row = emotion_row(factory, f"{batch_id}:task_001")
        assert row is not None and row.source_type == "batch"
        assert row.customer_name  # 客户昵称（split_customers 首个客侧 speaker）
        summary = json.loads(row.summary_json)
        assert summary["current"]["emotion"] == "中性"
        assert summary["changes"]["improved"] == 1  # 焦虑→中性
        assert [p["assistant_name"] for p in summary["per_assistant"]] == ["段勇亮"]  # 仅段勇亮有可评估前后对

    def test_emotion_llm_failure_still_completed(self, env, monkeypatch):
        """情绪 LLM 失败：任务仍 completed、无情绪行、emotion_id=None（不影响评分）。"""
        from tests.conftest import MockLLMByUserClient, overview_llm_json

        factory = env
        raw = (
            "客户甲\n2026-08-01 10:00:00\n有点慌\n\n"
            "韩珂龙头班\n2026-08-01 10:01:00\n您好，帮您看看\n\n"
            "客户甲\n2026-08-01 10:02:00\n好的谢谢\n\n"
            "李金潓\n2026-08-01 10:03:00\n不客气\n"
        )
        batch_id, _ = make_batch(
            factory,
            raw,
            employees=[("段勇亮", "E003"), ("李金潓", "E004")],
            name_map={"韩珂龙头班": "段勇亮"},
        )
        routes = [
            ("请按系统提示输出总览 JSON", overview_llm_json()),
            ("请对以下客户消息逐条标注情绪", LLMError("network", "mock 情绪网络错误")),
            ("员工姓名", valid_llm_json()),
        ]
        mock_runtime(monkeypatch, MockLLMByUserClient(routes))
        with factory() as s:
            task = brepo.list_tasks(s, batch_id)[0]
        import asyncio

        asyncio.run(mgr._run_task(task))
        status, retry, error, result_json = task_status(factory, batch_id)
        assert status == "completed" and error is None
        result = json.loads(result_json)
        assert result["emotion_id"] is None
        assert len(result["reports"]) == 2  # 评分报告不受影响
        assert emotion_row(factory, f"{batch_id}:task_001") is None

    def test_emotion_save_failure_still_completed(self, env, monkeypatch):
        """情绪落库失败（UNIQUE 冲突模拟）：session 回滚后任务仍 completed。"""
        from tests.conftest import MockLLMByUserClient, overview_llm_json

        factory = env
        raw = (
            "客户甲\n2026-08-01 10:00:00\n有点慌\n\n"
            "韩珂龙头班\n2026-08-01 10:01:00\n您好，帮您看看\n\n"
            "客户甲\n2026-08-01 10:02:00\n好的谢谢\n\n"
            "李金潓\n2026-08-01 10:03:00\n不客气\n"
        )
        batch_id, _ = make_batch(
            factory,
            raw,
            employees=[("段勇亮", "E003"), ("李金潓", "E004")],
            name_map={"韩珂龙头班": "段勇亮"},
        )
        routes = [
            ("请按系统提示输出总览 JSON", overview_llm_json()),
            ("请对以下客户消息逐条标注情绪", emotion_items_json([1, 3])),
            ("员工姓名", valid_llm_json()),
        ]
        mock_runtime(monkeypatch, MockLLMByUserClient(routes))

        import sqlite3

        def boom(*a, **k):
            raise sqlite3.IntegrityError("UNIQUE constraint failed: emotion_sessions.conversation_id")

        monkeypatch.setattr("backend.services.emotion.analyzer.repository.save_emotion_session", boom)
        with factory() as s:
            task = brepo.list_tasks(s, batch_id)[0]
        import asyncio

        asyncio.run(mgr._run_task(task))
        status, retry, error, result_json = task_status(factory, batch_id)
        assert status == "completed" and retry == 0 and error is None
        result = json.loads(result_json)
        assert result["emotion_id"] is None
        assert len(result["reports"]) == 2  # 报告照常落库（各自独立事务）
        assert result["overview_id"]  # 总览也不受影响
