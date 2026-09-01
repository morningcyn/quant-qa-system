# 多人质检分发器：完整会话 → 解析/识别/分段 → 并发调用单助理评分 → 总览汇总（上游模块）
# 原单助理评分逻辑（pipeline.run_inspection）零改动，仅追加可选参数。
import asyncio
import uuid

from sqlalchemy.orm import Session, sessionmaker

from backend.db import repository
from backend.schemas.overview import OverviewResult
from backend.services import multiparser, pipeline, prompts, report as report_service
from backend.services.llm import factory, json_guard
from backend.services.llm.base import LLMError
from backend.utils.errors import BizError


def _exc_info(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, BizError):
        return exc.code, exc.message
    if isinstance(exc, LLMError):
        return exc.code, exc.message
    return "unknown", str(exc)


async def run_multi_inspection(
    session: Session,
    raw_text: str,
    title: str | None,
    mapping: dict[str, int],
    conversation_id: str | None = None,
    client=None,
    cfg: dict | None = None,
) -> dict:
    """多人质检批次入口：返回 {conversation_id, overview_id, reports, errors, warnings}。

    mapping：助理规范名（canonical_name）→ 员工 id，必须覆盖全部识别出的助理；
    多出的键或指向不存在员工的键均为错误（宁缺毋滥，不让规则猜测归属）。
    """
    # ① 解析 + 识别 + 分段（纯规则）
    #    必须与预览（parse.preview_multi）用同一份 name_map：否则两次解析的
    #    canonical_name 不一致，mapping 校验会误报"以下助理尚未指定归属员工"
    result = multiparser.parse_multi(
        raw_text,
        repository.list_assistants(session),
        multiparser.load_name_map(),
        multiparser.load_not_assistant_names(),
    )
    clusters = result.clusters
    # ② 归属校验（前端预览已完成选择，后端二次兜底）
    names = {c.canonical_name for c in clusters}
    missing = names - set(mapping)
    if missing:
        raise BizError("unmapped_assistant", f"以下助理尚未指定归属员工：{'、'.join(sorted(missing))}", status_code=400)
    extra = set(mapping) - names
    if extra:
        raise BizError("validation_error", f"归属指定包含不存在的助理：{'、'.join(sorted(extra))}", status_code=400)
    for aid in dict.fromkeys(mapping.values()):
        if repository.get_assistant(session, aid) is None:
            raise BizError("not_found", f"员工不存在（id={aid}）", status_code=404)
    conversation_id = conversation_id or uuid.uuid4().hex
    owns_client = client is None
    if owns_client:
        client, cfg = factory.get_active_runtime(session)
    # ③ 并发分发：每簇一段独立质检（共享 client/cfg；save_inspection 同步无 await，不会交错）
    task_session_factory = sessionmaker(
        bind=session.get_bind(),
        autoflush=False,
        expire_on_commit=False,
    )

    async def run_one_cluster(cluster):
        with task_session_factory() as task_session:
            return await pipeline.run_inspection(
                task_session,
                mapping[cluster.canonical_name],
                cluster.segment.text,
                title,
                evaluatee=cluster.display_name,
                client=client,
                cfg=cfg,
                pre_parsed_turns=cluster.segment.turns,
                context_text=cluster.segment.context_text,
            )

    tasks = []
    for c in clusters:
        tasks.append(
            run_one_cluster(c)
        )
    results = await asyncio.gather(*tasks, return_exceptions=True)
    reports, errors = [], []
    for c, res in zip(clusters, results):
        if isinstance(res, Exception):
            code, message = _exc_info(res)
            errors.append(
                {"canonical_name": c.canonical_name, "display_name": c.display_name, "code": code, "message": message}
            )
            continue
        repository.set_inspection_conversation(session, res.id, conversation_id)
        inspection = repository.get_inspection(session, res.id)
        if inspection is None:
            errors.append(
                {
                    "canonical_name": c.canonical_name,
                    "display_name": c.display_name,
                    "code": "persistence_error",
                    "message": "质检结果已生成，但主会话无法读取结果",
                }
            )
            continue
        view = report_service.build_report_view(session, inspection)
        view["canonical_name"] = c.canonical_name
        view["reply_count"] = len(c.reply_turn_nos)
        view["reply_turn_range"] = f"{c.reply_turn_nos[0]}-{c.reply_turn_nos[-1]}" if c.reply_turn_nos else ""
        reports.append(view)
    if not reports:
        if owns_client:
            await client.aclose()
        raise BizError("multi_all_failed", "全部助理质检失败，未生成任何报告，请检查模型配置后重试", status_code=400)
    # ④ 总览（LLM 一次汇总；失败规则降级）
    overview = await generate_overview(session, reports, result.raw_text, title, conversation_id, client, cfg)
    if owns_client:
        await client.aclose()
    return {
        "conversation_id": conversation_id,
        "overview_id": overview.id,
        "reports": reports,
        "errors": errors,
        "warnings": result.warnings,
    }


async def generate_overview(
    session: Session,
    reports: list[dict],
    raw_dialogue: str,
    title: str | None,
    conversation_id: str,
    client,
    cfg: dict,
) -> object:
    """总览生成：参与者确定性聚合 → LLM 一次汇总 → 失败规则降级 → 落库（完整原始文本）。"""
    participants = [_participant_of(r) for r in reports]
    degraded = False
    try:
        ov = await json_guard.complete_json(
            client,
            prompts.build_overview_system_prompt(),
            prompts.build_overview_user_prompt(title, participants, raw_dialogue),
            OverviewResult,
            retries=2,
            temperature=0.2,
        )
        summary = ov.model_dump()
    except Exception:  # noqa: BLE001 总览 LLM 失败 → 规则降级，报告不受影响
        summary = _fallback_overview(participants)
        degraded = True
    summary["participants"] = participants
    return repository.save_overview(
        session,
        conversation_id,
        title,
        raw_dialogue,
        {"summary": summary, "participants": participants, "degraded": degraded},
        degraded,
        [r["id"] for r in reports],
    )


def _participant_of(r: dict) -> dict:
    """报告视图 → 总览参与者摘要（确定性聚合，供 LLM/规则使用）。"""
    quotes = []
    for h in r.get("highlight_dialogue") or []:
        if len(quotes) >= 3:
            break
        t, o = h.get("issue_type") or "", h.get("original_text") or ""
        quotes.append(f"{t}：{o}" if t else o)
    return {
        "assistant_id": r["assistant_id"],
        "name": r["assistant_name"],
        "employee_no": r["employee_no"],
        "total_score": r["total_score"],
        "is_red_alert": r["is_red_alert"],
        "red_alert_reasons": r["red_alert_reasons"],
        "is_yellow_alert": r["is_yellow_alert"],
        "yellow_alert_reasons": r["yellow_alert_reasons"],
        "customer_profile": r["customer_profile"],
        "reply_count": r.get("reply_count", r["turn_count"]),
        "turn_range": r.get("reply_turn_range", ""),
        "top_issue_quotes": quotes,
        "suggestions": r.get("improvement_suggestions") or [],
        "inspection_id": r["id"],
    }


def _fallback_overview(participants: list[dict]) -> dict:
    """规则降级总览（LLM 不可用）：优点/问题按确定性规则拼接，解决问题状态保守判定。"""
    strengths, issues = [], []
    for p in participants:
        label = f"{p['name']}（{p['total_score']}分）"
        if not p["red_alert_reasons"] and p["total_score"] >= 59:
            strengths.append(f"{label}：完成 {p['reply_count']} 次回复，整体服务达标")
        for why in p["red_alert_reasons"]:
            issues.append(f"{label} 红灯：{why}")
        for why in p["yellow_alert_reasons"]:
            issues.append(f"{label} 黄灯：{why}")
    any_red = any(p["red_alert_reasons"] for p in participants)
    if any_red:
        resolved, reason = "否", "存在红灯（合规红线命中），判定客户问题未得到合规解决。"
    else:
        resolved, reason = "无法判断", "规则无法确定客户问题是否最终解决，请结合各助理质检报告人工复核。"
    return {
        "main_strengths": strengths,
        "main_issues": issues,
        "customer_issue_resolved": resolved,
        "resolution_reason": reason,
        "overall_comment": "总览由规则自动生成（LLM 汇总不可用）：请结合各助理质检报告人工复核。",
    }
