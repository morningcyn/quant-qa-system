# Batch Task Manager：后台 asyncio worker 驱动任务状态机（uvicorn 单事件循环内 create_task）
# pending ─认领→ processing ─成功→ completed；可重试错误 → retrying 退避后回 processing
# 3 次执行全失败 → failed（手动「重新评分失败任务」重置重跑，重试计数归零）
# 致命错误（auth/未配置/解析失败/未匹配员工）直接 failed；completed 不重跑；stale processing 重启时重置
import asyncio
import json
import logging
import weakref
from datetime import timedelta

from sqlalchemy.orm import Session

from backend.db import batch_repository as brepo
from backend.db import repository
from backend.db.database import SessionLocal
from backend.services import multiparser, report as report_service, scoring
from backend.services.emotion import analyzer as emotion_analyzer
from backend.services.batch import aggregator, chunker
from backend.services.batch.splitter import dict_to_message
from backend.services.dispatcher import generate_overview
from backend.services.llm import factory
from backend.services.llm.base import LLMError
from backend.utils.errors import BizError

logger = logging.getLogger(__name__)

# 致命错误：重试也不会成功 → 直接 failed（等用户修复后手动重跑）
FATAL_CODES = {"auth", "not_configured", "parse_failed", "unmapped_assistant"}
# 模型鉴权/配置错误对整个批次都成立；解析或员工归属问题只影响当前任务，
# 仍应允许批次继续处理其他客户。
GLOBAL_FATAL_CODES = {"auth", "not_configured"}
# 可重试错误退避（秒）：第 1 次失败等 5s，第 2 次 15s，第 3 次 30s 后 failed
RETRY_DELAYS = [5, 15, 30]
MAX_ATTEMPTS = 3


class BatchTaskManager:
    """批量评分任务管理器：全局并发上限 Semaphore(3)，每任务独立 SessionLocal。"""

    MAX_CONCURRENT_TASKS = 3
    _semaphores = weakref.WeakKeyDictionary()
    _workers: dict[str, asyncio.Task] = {}

    @classmethod
    def _get_semaphore(cls) -> asyncio.Semaphore:
        """Return the concurrency limiter belonging to the current event loop."""
        loop = asyncio.get_running_loop()
        semaphore = cls._semaphores.get(loop)
        if semaphore is None:
            semaphore = asyncio.Semaphore(cls.MAX_CONCURRENT_TASKS)
            cls._semaphores[loop] = semaphore
        return semaphore

    def start_batch(self, batch_id: str) -> bool:
        """幂等启动：已有活跃 worker 或批次已全部完成 → False。"""
        existing = self._workers.get(batch_id)
        if existing is not None and not existing.done():
            return False
        self._workers[batch_id] = asyncio.get_running_loop().create_task(self._worker(batch_id))
        return True

    def stop_batch(self, batch_id: str) -> None:
        """删除批次前停止其后台 worker（取消任务，执行中的任务下次重启会被 stale 重置）。"""
        task = self._workers.pop(batch_id, None)
        if task is not None and not task.done():
            task.cancel()

    def resume_all(self) -> None:
        """断点续跑：重启后对仍有未完成任务（pending/processing/retrying）的批次启动 worker。
        completed 不重跑；failed 不自动重跑（等用户手动修复后点按钮）。"""
        with SessionLocal() as session:
            runs, _ = brepo.list_batches(session, page_size=1000)
            for run in runs:
                counts = brepo.count_tasks_by_status(session, run.batch_id)
                if counts["pending"] + counts["processing"] + counts["retrying"] > 0:
                    self.start_batch(run.batch_id)

    async def _worker(self, batch_id: str) -> None:
        """循环认领 pending 任务；无 pending 且无执行中任务 → 批次置 done 并退出。

        每轮先做一次 stale 重置（强杀快速重启时 processing 未满 10 分钟，先空转等待，
        到点自动重置续跑；正常完成路径不产生额外等待）。
        """
        try:
            while True:
                with SessionLocal() as session:
                    brepo.reset_stale_processing(session, older_than=timedelta(minutes=10))
                    task_obj = brepo.claim_next_pending(session, batch_id)
                    if task_obj is None:
                        if brepo.is_batch_finished(session, batch_id):
                            brepo.set_batch_status(session, batch_id, "done")
                            return
                        counts = brepo.count_tasks_by_status(session, batch_id)
                        if counts["processing"] + counts["retrying"] == 0:
                            return  # 防御：无 pending 也无执行中 → 不该发生，直接退出
                if task_obj is None:
                    await asyncio.sleep(5)  # 遗留 processing 未到 stale 阈值 → 等其到点自动重置
                    continue
                await self._run_task(task_obj)
        except Exception:  # noqa: BLE001 worker 不应因单任务异常退出
            logger.exception("batch worker 异常退出: %s", batch_id)

    async def _run_task(self, task_obj) -> None:
        """单任务执行 + 重试：每轮独立 SessionLocal，最多 MAX_ATTEMPTS 次执行。"""
        batch_id, task_id = task_obj.batch_id, task_obj.task_id
        while True:
            session = SessionLocal()
            try:
                task = brepo.get_task(session, batch_id, task_id)
                if task is None:
                    return
                try:
                    async with self._get_semaphore():
                        result_json = await self._execute(session, task)
                except (BizError, LLMError) as exc:
                    code, message = exc.code, exc.message
                    if code in FATAL_CODES:
                        brepo.set_task_status(session, task, "failed", error=message, retry_count=task.retry_count)
                        if code in GLOBAL_FATAL_CODES:
                            # 401/未配置不会因更换客户而恢复，立即结束剩余任务，
                            # 避免一个批次重复消耗大量无效请求。
                            brepo.fail_unfinished_tasks(session, batch_id, message)
                        return
                    task.retry_count += 1
                    if task.retry_count >= MAX_ATTEMPTS:
                        brepo.set_task_status(session, task, "failed", error=message, retry_count=task.retry_count)
                        return
                    delay = RETRY_DELAYS[min(task.retry_count - 1, len(RETRY_DELAYS) - 1)]
                    brepo.set_task_status(session, task, "retrying", error=message, retry_count=task.retry_count)
                except Exception as exc:  # noqa: BLE001 未知异常按可重试处理
                    logger.exception("batch task 未知异常: %s/%s", batch_id, task_id)
                    task.retry_count += 1
                    if task.retry_count >= MAX_ATTEMPTS:
                        brepo.set_task_status(session, task, "failed", error=f"未知错误：{exc}", retry_count=task.retry_count)
                        return
                    delay = RETRY_DELAYS[min(task.retry_count - 1, len(RETRY_DELAYS) - 1)]
                    brepo.set_task_status(session, task, "retrying", error=f"未知错误：{exc}", retry_count=task.retry_count)
                else:
                    brepo.set_task_status(session, task, "completed", error=None, result_json=result_json)
                    return
                session.rollback()  # 未提交脏状态丢弃（已 commit 的 inspection 保留）
            finally:
                session.close()
            await asyncio.sleep(delay)

    async def _execute(self, session: Session, task) -> dict:
        owned_client = None

        def load_runtime():
            nonlocal owned_client
            owned_client, cfg = factory.get_active_runtime(session)
            return owned_client, cfg

        try:
            return await self._execute_with_client(session, task, load_runtime)
        finally:
            if owned_client is not None:
                await owned_client.aclose()

    async def _execute_with_client(self, session: Session, task, load_runtime) -> dict:
        """评分一个客户会话：重建消息 → 执行时员工匹配 → 每助理（簇）chunk 评分 → 落库。"""
        data = json.loads(task.input_data)
        msgs = [dict_to_message(m) for m in data.get("messages", [])]
        if not msgs:
            raise BizError("parse_failed", "该任务会话消息为空，无法评分")
        # ① 执行时重跑员工匹配（员工档案可能已更新；与多人质检同一套确定性聚类）
        result = multiparser.MultiParseResult(messages=msgs)
        employees = repository.list_assistants(session)
        clusters = multiparser._cluster_assistants(msgs, employees)
        if not clusters:
            raise BizError("parse_failed", "未识别到助理发言，请检查该会话的聊天记录格式")
        multiparser._build_segments(result, clusters)
        # ② 未匹配员工 → 任务 failed 并引导（不落任何报告，重跑时干净）
        unmatched = [c.canonical_name for c in clusters if not c.assistant_id]
        if unmatched:
            raise BizError(
                "unmapped_assistant",
                f"以下助理未匹配到员工档案：{'、'.join(unmatched)}。"
                "请先在「员工档案」创建对应员工，再点「重新评分失败任务」",
            )
        # ③ LLM 运行时（not_configured → 致命）
        client, cfg = load_runtime()
        chunk_params = repository.get_setting_json(session, "batch_chunk_params") or {}
        title = data.get("title")
        reports: list[dict] = []
        errors: list[dict] = []
        for cluster in clusters:
            assistant = repository.get_assistant(session, cluster.assistant_id)
            try:
                reports.append(
                    await self._score_one_cluster(session, assistant, cluster, client, cfg, title, chunk_params, task.batch_id)
                )
            except (BizError, LLMError) as exc:
                errors.append(
                    {
                        "canonical_name": cluster.canonical_name,
                        "display_name": cluster.display_name,
                        "code": exc.code,
                        "message": exc.message,
                    }
                )
        if not reports:
            # 全部失败：任一错误是致命（auth/未配置）→ 透传致命码（重试不会成功）；否则可重试
            fatal = next((e for e in errors if e["code"] in FATAL_CODES), None)
            if fatal:
                raise BizError(fatal["code"], fatal["message"])
            raise BizError("multi_all_failed", "该会话全部助理质检失败，未生成任何报告，请检查模型配置后重试")
        # ④ 多助理会话生成总览（复用多人质检同一总览：LLM 一次汇总，失败规则降级）。
        #    单助理任务不生成——总览用于评分对比与优缺点提炼，一份报告无对比意义。
        #    注意：service_overviews.conversation_id 有 UNIQUE 约束，批次内任务共享 batch_id
        #    会导致第二个多助理任务落库冲突（卡死）。总览查看一律按 overview_id 跳转，
        #    conversation_id 仅需唯一 → 使用「batch_id:task_id」每任务独立锚点。
        overview_id = None
        if len(reports) > 1:
            try:
                views = []
                for r in reports:
                    ins = repository.get_inspection(session, r["inspection_id"])
                    views.append(report_service.build_report_view(session, ins))
                raw_text = "\n".join(f"#{m.turn_no} {m.speaker}：{m.text}" for m in msgs)
                ov = await generate_overview(
                    session, views, raw_text, title, f"{task.batch_id}:{task.task_id}", client, cfg
                )
                overview_id = ov.id
            except Exception:  # noqa: BLE001 总览失败不影响报告落库（报告已生成可查看）
                logger.exception("batch 总览生成失败: %s/%s", task.batch_id, task.task_id)
                session.rollback()  # 落库失败会污染 session（PendingRollback），回滚后任务状态才能正常更新
        # ⑤ 客户情绪分析（一次性：失败绝不影响任务状态，只记日志；落库失败回滚防 PendingRollback）。
        #    情绪锚点用「batch_id:task_id」每任务独立（emotion_sessions.conversation_id 有 UNIQUE 约束，
        #    批次内任务共享 batch_id 会冲突；与总览锚点规则一致）。
        emotion_id = None
        try:
            emo = await emotion_analyzer.analyze_session(
                session,
                msgs=msgs,
                title=title,
                conversation_id=f"{task.batch_id}:{task.task_id}",
                source_type="batch",
                customer_name=task.customer_name,
                client=client,
                cfg=cfg,
            )
            emotion_id = emo.id if emo else None  # emo=None = 会话无客户消息，静默跳过
        except Exception:  # noqa: BLE001 情绪分析失败不影响评分任务（报告已生成可查看）
            logger.exception("batch 情绪分析失败: %s/%s", task.batch_id, task.task_id)
            session.rollback()
        return {
            "reports": reports,
            "errors": errors,
            "chunk_count": sum(r["chunk_count"] for r in reports),
            "overview_id": overview_id,
            "emotion_id": emotion_id,
        }
        client, cfg = load_runtime()
    async def _score_one_cluster(self, session, assistant, cluster, client, cfg, title, chunk_params, batch_id) -> dict:
        """单助理（簇）：切 chunk → 评分/汇总 → 落库（conversation_id=batch_id，与多人质检同一锚点）。"""
        segment = cluster.segment
        chunks = chunker.chunk_segment(segment, chunk_params)
        merged, degraded = await aggregator.aggregate_chunks(
            session, chunks, assistant, cluster.display_name, client, cfg, title, assistant.template_type
        )
        template = scoring.load_template(session, assistant.template_type)
        profile = (merged.d_scores.d2_profile_match.profile or "").strip() or None
        ins = repository.save_inspection(
            session,
            assistant_id=assistant.id,
            session_title=(title or "").strip() or None,
            total_score=merged.total_score,
            is_yellow_alert=merged.is_yellow_alert,
            yellow_alert_reasons=merged.yellow_alert_reasons,
            is_red_alert=merged.is_red_alert,
            red_alert_reasons=merged.red_alert_reasons,
            template_type=assistant.template_type,
            template_snapshot=template,
            turn_count=sum(len(c.turns) for c in chunks),
            customer_profile=profile,
            raw_dialogue=segment.text,
            d_scores=merged.d_scores.model_dump(),
            s_scores=merged.s_scores.model_dump(),
            highlight_dialogue=[h.model_dump() for h in merged.highlight_dialogue],
            suggestions=merged.improvement_suggestions,
            evaluatee=cluster.display_name,
            na_dims=getattr(merged, "na_dims", None),
            effective_max=getattr(merged, "effective_max", None),
        )
        repository.set_inspection_conversation(session, ins.id, batch_id)
        return {
            "assistant_id": ins.assistant_id,
            "assistant_name": assistant.name,
            "employee_no": assistant.employee_no,
            "inspection_id": ins.id,
            "total_score": ins.total_score,
            "is_red_alert": ins.is_red_alert,
            "turn_count": ins.turn_count,
            "degraded": degraded,
            "chunk_count": len(chunks),
        }


mgr = BatchTaskManager()
