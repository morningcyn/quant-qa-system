# 批量评分数据访问层：BatchRun / BatchTask 的读写（独立文件，不污染 repository.py）
# 每个方法自管 commit；调用方持有 SessionLocal 会话（每任务独立会话，互不干扰）
import json
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from backend.db.models import BatchRun, BatchTask

TASK_STATUSES = ("pending", "processing", "retrying", "completed", "failed")
BATCH_STATUSES = ("pending", "running", "done")


# ---------- BatchRun ----------

def create_batch_run(session: Session, batch_id: str, title: str | None, source_stats: dict, warnings: list) -> BatchRun:
    run = BatchRun(
        batch_id=batch_id,
        title=title,
        status="pending",
        source_stats_json=json.dumps(source_stats, ensure_ascii=False),
        warnings_json=json.dumps(warnings, ensure_ascii=False),
    )
    session.add(run)
    session.commit()
    return run


def get_batch(session: Session, batch_id: str) -> BatchRun | None:
    return session.scalar(select(BatchRun).where(BatchRun.batch_id == batch_id))


def list_batches(session: Session, page: int = 1, page_size: int = 20) -> tuple[list[BatchRun], int]:
    total = session.scalar(select(func.count(BatchRun.id))) or 0
    rows = list(
        session.scalars(
            select(BatchRun).order_by(BatchRun.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        )
    )
    return rows, total


def set_batch_status(session: Session, batch_id: str, status: str) -> BatchRun | None:
    if status not in BATCH_STATUSES:
        raise ValueError(f"非法批次状态: {status}")
    run = get_batch(session, batch_id)
    if run:
        run.status = status
        session.commit()
    return run


# ---------- BatchTask ----------

def create_tasks(session: Session, batch_id: str, tasks: list[dict]) -> list[BatchTask]:
    """批量建任务：一次 add 一次 commit（导入时整批落库）。"""
    objs = [
        BatchTask(
            batch_id=batch_id,
            task_id=t["task_id"],
            customer_id=t["customer_id"],
            customer_name=t["customer_name"],
            assistant_ids_json=json.dumps(t.get("assistant_ids", []), ensure_ascii=False),
            input_data=json.dumps(t["input_data"], ensure_ascii=False),
            status="pending",
        )
        for t in tasks
    ]
    session.add_all(objs)
    session.commit()
    return objs


def list_tasks(session: Session, batch_id: str) -> list[BatchTask]:
    return list(
        session.scalars(select(BatchTask).where(BatchTask.batch_id == batch_id).order_by(BatchTask.id))
    )


def get_task(session: Session, batch_id: str, task_id: str) -> BatchTask | None:
    return session.scalar(
        select(BatchTask).where(BatchTask.batch_id == batch_id, BatchTask.task_id == task_id)
    )


def claim_next_pending(session: Session, batch_id: str) -> BatchTask | None:
    """原子认领下一条 pending 任务（SQLite 写锁串行化，无需 with_for_update）。"""
    task = session.scalar(
        select(BatchTask)
        .where(BatchTask.batch_id == batch_id, BatchTask.status == "pending")
        .order_by(BatchTask.id)
        .limit(1)
    )
    if task is None:
        return None
    task.status = "processing"
    task.updated_at = datetime.now()
    session.commit()
    return task


def set_task_status(
    session: Session,
    task: BatchTask,
    status: str,
    error: str | None = None,
    result_json: dict | None = None,
    retry_count: int | None = None,
) -> None:
    if status not in TASK_STATUSES:
        raise ValueError(f"非法任务状态: {status}")
    task.status = status
    if error is not None:
        task.error = error
    if result_json is not None:
        task.result_json = json.dumps(result_json, ensure_ascii=False)
    if retry_count is not None:
        task.retry_count = retry_count
    task.updated_at = datetime.now()
    session.commit()


def count_tasks_by_status(session: Session, batch_id: str) -> dict[str, int]:
    rows = session.execute(
        select(BatchTask.status, func.count(BatchTask.id))
        .where(BatchTask.batch_id == batch_id)
        .group_by(BatchTask.status)
    ).all()
    counts = {s: 0 for s in TASK_STATUSES}
    for status, count in rows:
        counts[status] = count
    return counts


def is_batch_finished(session: Session, batch_id: str) -> bool:
    counts = count_tasks_by_status(session, batch_id)
    total = sum(counts.values())
    return total > 0 and counts["pending"] == 0 and counts["processing"] == 0 and counts["retrying"] == 0


def reset_stale_processing(session: Session, older_than: timedelta = timedelta(minutes=10)) -> int:
    """断点续跑：处理中超时（进程被强杀遗留）→ 置回 pending 重新认领。"""
    cutoff = datetime.now() - older_than
    result = session.execute(
        update(BatchTask)
        .where(BatchTask.status.in_(["processing", "retrying"]), BatchTask.updated_at < cutoff)
        .values(status="pending", updated_at=datetime.now())
    )
    session.commit()
    return result.rowcount or 0


def reset_failed_tasks(session: Session, batch_id: str) -> int:
    """手动重跑失败任务：failed → pending，重试计数归零。"""
    result = session.execute(
        update(BatchTask)
        .where(BatchTask.batch_id == batch_id, BatchTask.status == "failed")
        .values(status="pending", retry_count=0, error=None, updated_at=datetime.now())
    )
    session.commit()
    return result.rowcount or 0


def fail_unfinished_tasks(session: Session, batch_id: str, error: str) -> int:
    """批次级致命错误：将本批次所有未完成任务统一置为 failed。

    API Key 无效/未配置属于全局错误，继续逐条请求只会重复得到相同失败；
    统一失败后，用户修复模型配置即可通过「重新评分失败任务」恢复处理。
    """
    result = session.execute(
        update(BatchTask)
        .where(
            BatchTask.batch_id == batch_id,
            BatchTask.status.in_(["pending", "processing", "retrying"]),
        )
        .values(status="failed", error=error, updated_at=datetime.now())
    )
    session.commit()
    return result.rowcount or 0


def delete_batch(session: Session, batch_id: str) -> int:
    """删除批次及其任务；关联质检报告（conversation_id=batch_id）连同明细一并删除（无级联，先删明细防孤儿行）。"""
    from sqlalchemy import delete

    from backend.db.models import Inspection, InspectionDetail

    run = get_batch(session, batch_id)
    if run is None:
        return 0
    ins_ids = [row[0] for row in session.execute(select(Inspection.id).where(Inspection.conversation_id == batch_id)).all()]
    if ins_ids:
        session.execute(delete(InspectionDetail).where(InspectionDetail.inspection_id.in_(ins_ids)))
        session.execute(delete(Inspection).where(Inspection.id.in_(ins_ids)))
    session.execute(delete(BatchTask).where(BatchTask.batch_id == batch_id))
    session.delete(run)
    session.commit()
    return 1


def task_stats(session: Session, batch_id: str) -> dict:
    """进度条统计：总数 + 各状态计数 + 完成占比。"""
    counts = count_tasks_by_status(session, batch_id)
    total = sum(counts.values())
    done = counts["completed"] + counts["failed"]
    return {
        "total": total,
        "done": done,
        "completed": counts["completed"],
        "failed": counts["failed"],
        "pending": counts["pending"],
        "processing": counts["processing"],
        "retrying": counts["retrying"],
        "percent": round(done * 100 / total) if total else 0,
    }
