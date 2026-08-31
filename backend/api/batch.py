# 批量评分 API：导入 → 任务列表 → 后台评分 → 进度轮询 → 失败重跑
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.db import batch_repository as brepo
from backend.db import repository
from backend.db.database import get_db
from backend.schemas.batch import BatchImportIn
from backend.services import multiparser
from backend.services.batch.manager import mgr
from backend.services.batch.splitter import message_to_dict, split_customers

router = APIRouter(prefix="/batch", tags=["batch"])


@router.post("/import")
def import_batch(body: BatchImportIn, session: Session = Depends(get_db)):
    """整批聊天记录 → 自动按客户昵称切分 → 建批次 + 每客户一条任务（pending）。
    解析失败不 400：建 1 个任务，执行时失败并显示解析错误（导入总是成功）。"""
    split = split_customers(
        body.raw_text,
        repository.list_assistants(session),
        multiparser.load_name_map(),
        multiparser.load_not_assistant_names(),
        rooms=body.rooms,
    )
    batch_id = uuid.uuid4().hex
    title = (body.title or "").strip() or None
    source_stats = {
        "customer_count": len(split["customers"]),
        "assistant_count": split["assistant_count"],
        "task_count": split["task_count"],
        "message_count": split["message_count"],
    }
    run = brepo.create_batch_run(session, batch_id, title, source_stats, split["warnings"])
    tasks = [
        {
            "task_id": f"task_{i + 1:03d}",
            "customer_id": c.customer_id,
            "customer_name": c.customer_name,
            "assistant_ids": [],  # 执行时重跑员工匹配后再回填（assistant_ids_json 运行时更新）
            "input_data": {
                "messages": [message_to_dict(m) for m in c.messages],
                "title": title,
                "source_fmt": "rooms" if body.rooms else "text",
            },
        }
        for i, c in enumerate(split["customers"])
    ]
    brepo.create_tasks(session, batch_id, tasks)
    return {
        "batch_id": batch_id,
        "title": title,
        "status": run.status,
        "source_stats": source_stats,
        "task_count": split["task_count"],
        "customer_count": len(split["customers"]),
        "assistant_count": split["assistant_count"],
        "message_count": split["message_count"],
        "warnings": split["warnings"],
        "parse_error": split.get("parse_error"),
        "customers": [
            {
                "customer_id": c.customer_id,
                "customer_name": c.customer_name,
                "message_count": c.message_count,
                "assistant_names": c.assistant_names,
            }
            for c in split["customers"]
        ],
    }


@router.post("/{batch_id}/start")
async def start_batch(batch_id: str, session: Session = Depends(get_db)):
    """开始批量评分（幂等）：启动后台 worker 逐任务评分。"""
    run = brepo.get_batch(session, batch_id)
    if run is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    if run.status == "done" and brepo.count_tasks_by_status(session, batch_id)["pending"] == 0:
        return {"batch_id": batch_id, "started": False, "message": "批次已完成"}
    brepo.set_batch_status(session, batch_id, "running")
    started = mgr.start_batch(batch_id)
    return {"batch_id": batch_id, "started": started}


def _task_item(task, current_emotion: dict | None = None) -> dict:
    result = json.loads(task.result_json) if task.result_json else None
    reports = (result or {}).get("reports") or []
    data = json.loads(task.input_data) if task.input_data else {}
    return {
        "task_id": task.task_id,
        # 客户当前情绪摘要（批量评分时已自动分析；未生成/失败为 null）——进度页任务行标签
        "current_emotion": current_emotion,
        "customer_id": task.customer_id,
        "customer_name": task.customer_name,
        "assistant_ids": json.loads(task.assistant_ids_json or "[]"),
        "message_count": len(data.get("messages") or []),
        "status": task.status,
        "score": reports[0]["total_score"] if reports else None,
        "scores": [r["total_score"] for r in reports],
        "error": task.error,
        "inspection_id": reports[0]["inspection_id"] if reports else None,
        "assistant_names": [r["assistant_name"] for r in reports],
        # 每份报告的跳转入口（一个客户会话可能多位助理各自成报告）：
        # 前端按助理逐条渲染分数链接，不再只进第一份
        "reports": [
            {
                "assistant_name": r["assistant_name"],
                "total_score": r["total_score"],
                "inspection_id": r["inspection_id"],
            }
            for r in reports
        ],
        "degraded": any(r.get("degraded") for r in reports),
        "retry_count": task.retry_count,
        "chunk_count": (result or {}).get("chunk_count"),
        # 多助理任务的本次客户服务总览（评分对比 + 优缺点；单助理任务为 null）
        "overview_id": (result or {}).get("overview_id"),
        "updated_at": task.updated_at.isoformat(timespec="seconds") if task.updated_at else None,
    }


@router.get("/{batch_id}/progress")
def batch_progress(batch_id: str, session: Session = Depends(get_db)):
    """进度轮询：批次状态 + 状态统计 + 任务明细（前端 2s 轮询）。"""
    run = brepo.get_batch(session, batch_id)
    if run is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    # 批量读出本批次全部情绪行（conversation_id=batch_id:task_id），避免每任务一次查询
    emotions = {
        emo.conversation_id: json.loads(emo.summary_json).get("current")
        for emo in repository.list_emotion_sessions_by_conversation_prefix(session, f"{batch_id}:")
    }
    return {
        "batch_id": run.batch_id,
        "title": run.title,
        "status": run.status,
        "stats": brepo.task_stats(session, batch_id),
        "source_stats": json.loads(run.source_stats_json or "{}"),
        "warnings": json.loads(run.warnings_json or "[]"),
        "items": [
            _task_item(t, current_emotion=emotions.get(f"{batch_id}:{t.task_id}"))
            for t in brepo.list_tasks(session, batch_id)
        ],
    }


@router.post("/{batch_id}/retry-failed")
async def retry_failed(batch_id: str, session: Session = Depends(get_db)):
    """重新评分失败任务：failed → pending（重试计数归零）→ 启动 worker。"""
    run = brepo.get_batch(session, batch_id)
    if run is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    reset_count = brepo.reset_failed_tasks(session, batch_id)
    if reset_count == 0:
        return {"batch_id": batch_id, "reset_count": 0, "started": False}
    brepo.set_batch_status(session, batch_id, "running")
    started = mgr.start_batch(batch_id)
    return {"batch_id": batch_id, "reset_count": reset_count, "started": started}


@router.delete("/{batch_id}")
async def delete_batch(batch_id: str, session: Session = Depends(get_db)):
    """删除批次（含任务与关联质检报告）：先停后台 worker 再删库。"""
    run = brepo.get_batch(session, batch_id)
    if run is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    mgr.stop_batch(batch_id)
    deleted = brepo.delete_batch(session, batch_id)
    return {"batch_id": batch_id, "deleted": deleted}


@router.get("")
def list_batches(page: int = 1, page_size: int = 20, session: Session = Depends(get_db)):
    """批次历史列表（倒序）。"""
    rows, total = brepo.list_batches(session, max(page, 1), max(min(page_size, 100), 1))
    return {
        "total": total,
        "batches": [
            {
                "batch_id": r.batch_id,
                "title": r.title,
                "status": r.status,
                "source_stats": json.loads(r.source_stats_json or "{}"),
                "warnings": json.loads(r.warnings_json or "[]"),
                "stats": brepo.task_stats(session, r.batch_id),
                "created_at": r.created_at.isoformat(timespec="seconds") if r.created_at else None,
            }
            for r in rows
        ],
    }
