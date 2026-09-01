# 质检流水线编排：解析 → 规则书 → 一次 LLM 主调用（含归因/改写/建议）→ guardrails → 落库
# 主调用校验失败按 L1(重试)→L2(降温)→L3(拆分) 降级；全失败不落库。
import json

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.db import repository
from backend.schemas.inspection import (
    DScores,
    HighlightItem,
    LLMResultSchema,
    SScores,
)
from backend.services import parser as parser_service
from backend.services import prompts, scoring
from backend.services.llm import factory, json_guard
from backend.services.llm.base import LLMError
from backend.utils.errors import BizError


class ScoringOnlySchema(BaseModel):
    """L3 拆分第一步：只出评分（含红灯一票否决，算术剥离由后端汇总）。"""

    model_config = {"extra": "ignore"}

    total_score: int = Field(default=0, ge=0, le=100)
    is_red_alert: bool = Field(default=False)
    red_alert_reasons: list[str] = Field(default_factory=list)
    is_yellow_alert: bool = Field(default=False)
    yellow_alert_reasons: list[str] = Field(default_factory=list)
    d_scores: DScores
    s_scores: SScores


class RewriteOnlySchema(BaseModel):
    """L3 拆分第二步：只出高亮改写与建议。"""

    model_config = {"extra": "ignore"}

    highlight_dialogue: list[HighlightItem] = Field(default_factory=list)
    improvement_suggestions: list[str] = Field(default_factory=list)


def _default_temperature(cfg: dict) -> float:
    try:
        return float(cfg.get("temperature", 0.2))
    except (TypeError, ValueError):
        return 0.2


async def _call_main_with_fallbacks(
    client, system: str, user: str, numbered_text: str, session_title: str | None, cfg: dict,
    teacher_persona: str | None = None, evaluatee: str | None = None,
) -> LLMResultSchema:
    """一次主调用 + L1/L2 降级；仍失败进入 L3 拆分。"""
    temperature = _default_temperature(cfg)
    try:
        return await json_guard.complete_json(
            client, system, user, LLMResultSchema, retries=3, temperature=temperature
        )
    except LLMError as exc:
        if exc.code != "bad_json":
            raise  # auth/网络/超时等直接上抛
    # L2：降温 + 精简重试（去掉次要字段约束，靠 extra=ignore 容忍）
    try:
        return await json_guard.complete_json(
            client, system, user, LLMResultSchema, retries=1, temperature=0.1
        )
    except LLMError as exc:
        if exc.code != "bad_json":
            raise
    # L3：拆分为 评分 + 改写 两次调用
    return await _call_split(client, system, user, numbered_text, session_title, cfg, teacher_persona, evaluatee)


async def _call_split(
    client, system: str, user: str, numbered_text: str, session_title: str | None, cfg: dict,
    teacher_persona: str | None = None, evaluatee: str | None = None,
) -> LLMResultSchema:
    """L3：调用 A 只出评分，调用 B 以评分结果为上下文出高亮改写与建议。"""
    temperature = _default_temperature(cfg)
    try:
        scoring_only = await json_guard.complete_json(
            client,
            prompts.build_scoring_only_system(_rulebook_of(system)),
            user,
            ScoringOnlySchema,
            retries=1,
            temperature=temperature,
        )
    except LLMError as exc:
        if exc.code != "bad_json":
            raise
        raise BizError("llm_failed", "大模型连续多次输出异常，本次质检未完成，请稍后重试或更换模型") from exc
    scoring_text = scoring_only.model_dump_json()
    try:
        rewrite = await json_guard.complete_json(
            client,
            prompts.build_rewrite_only_system(),
            prompts.build_rewrite_user_prompt(numbered_text, scoring_text, session_title, teacher_persona, evaluatee),
            RewriteOnlySchema,
            retries=1,
            temperature=temperature,
        )
        highlight = rewrite.highlight_dialogue
        suggestions = rewrite.improvement_suggestions
    except LLMError:
        highlight, suggestions = [], []  # 评分成功、改写失败：保留评分，报告仍可用
    return LLMResultSchema(
        total_score=scoring_only.total_score,
        is_red_alert=scoring_only.is_red_alert,
        red_alert_reasons=scoring_only.red_alert_reasons,
        is_yellow_alert=scoring_only.is_yellow_alert,
        yellow_alert_reasons=scoring_only.yellow_alert_reasons,
        d_scores=scoring_only.d_scores,
        s_scores=scoring_only.s_scores,
        highlight_dialogue=highlight,
        improvement_suggestions=suggestions,
    )


def _rulebook_of(system: str) -> str:
    """从完整系统提示词中截回规则书部分（L3 评分调用复用同一规则书）。"""
    marker = "## 质检评分规则书"
    idx = system.find(marker)
    if idx == -1:
        return ""
    end = system.find("## 输出协议", idx)
    return system[idx : end if end != -1 else len(system)].strip()


async def run_inspection(
    session: Session,
    assistant_id: int,
    raw_text: str,
    session_title: str | None = None,
    evaluatee: str | None = None,
    client=None,
    cfg: dict | None = None,
    pre_parsed_turns: list | None = None,
    context_dialogue: str | None = None,
    context_text: str | None = None,
):
    """完整质检流水线：返回 repository.Inspection（已落库）。

    pre_parsed_turns：多人质检分段的结构化轮次（保留绝对 turn_no 与原始 speaker），
    优先于 raw_text 正则解析——第一版编号文本 [n][speaker] 无法被 parse_raw 反解。
    context_dialogue / context_text：评估段落之外的对话（仅作衔接参考，不计分），
    两者皆缺省时行为与第一版完全一致。
    """
    assistant = repository.get_assistant(session, assistant_id)
    if assistant is None:
        raise BizError("not_found", "员工不存在", status_code=404)
    # ① 结构化解析（防呆硬错误在此抛出，不进模型）
    if pre_parsed_turns:
        parsed = parser_service.parse_turns(pre_parsed_turns)
    else:
        parsed = parser_service.parse_raw(raw_text)
    # 前置/后文上下文（多人轮替衔接）：编号文本直用（多段已生成），或解析原文（解析失败不影响质检）
    if context_text is None and (context_dialogue or "").strip():
        try:
            context_text = parser_service.to_numbered_text(parser_service.parse_raw(context_dialogue).turns)
        except Exception:  # noqa: BLE001 上下文解析失败不影响主对话质检
            context_text = None
    # 锁定本次评估对象：多角色对话只对指定助理计分（为空时按唯一助理推导，兜底"助理A"）
    if not (evaluatee or "").strip():
        evaluatee = (parsed.speakers or ["助理A"])[0]
    # ② 规则上下文
    template = scoring.load_template(session, assistant.template_type)
    rulebook = scoring.render_rulebook(template)
    system = prompts.build_system_prompt(rulebook)
    numbered_text = (
        parser_service.summarize_long_dialogue(parsed.turns)
        if len(parsed.turns) > 60
        else parser_service.to_numbered_text(parsed.turns)
    )
    teacher_persona = assistant.teacher_persona or None
    user = prompts.build_user_prompt(
        assistant.name, assistant.employee_no, template, numbered_text, session_title, teacher_persona, evaluatee,
        context_text=context_text,
    )
    # ③④⑤ 一次主调用（内含归因、黄金改写、建议），带 L1/L2/L3 降级
    owns_client = client is None
    if owns_client:
        client, cfg = factory.get_active_runtime(session)
    try:
        result = await _call_main_with_fallbacks(
            client, system, user, numbered_text, session_title, cfg, teacher_persona, evaluatee
        )
    except LLMError as exc:
        raise BizError(exc.code, exc.message, status_code=400) from exc
    finally:
        if owns_client:
            await client.aclose()
    # ②' 后端重算熔断与总分（不信任模型算术），红灯一票否决补全，N/A 维度动态分母折算
    # 多助理分段：turn_no 为原始绝对轮次，高亮轮次上界按实际最大轮次校验（与正文编号一致）
    result = scoring.apply_guardrails(result, template, len(parsed.turns), max_turn=max(t.turn_no for t in parsed.turns))
    profile = (result.d_scores.d2_profile_match.profile or "").strip() or None
    return repository.save_inspection(
        session,
        assistant_id=assistant.id,
        session_title=(session_title or "").strip() or None,
        total_score=result.total_score,
        is_yellow_alert=result.is_yellow_alert,
        yellow_alert_reasons=result.yellow_alert_reasons,
        is_red_alert=result.is_red_alert,
        red_alert_reasons=result.red_alert_reasons,
        template_type=assistant.template_type,
        template_snapshot=template,
        turn_count=len(parsed.turns),
        customer_profile=profile,
        raw_dialogue=raw_text.strip(),
        d_scores=result.d_scores.model_dump(),
        s_scores=result.s_scores.model_dump(),
        highlight_dialogue=[h.model_dump() for h in result.highlight_dialogue],
        suggestions=result.improvement_suggestions,
        evaluatee=evaluatee,
        na_dims=getattr(result, "na_dims", None),
        effective_max=getattr(result, "effective_max", None),
    )
