# 单 chunk 评分：复用 pipeline 内部主调用（不落库），结果仅作聚合输入
# 与 run_inspection 完全一致的 prompt 构建 + guardrails，输出契约零改动
from backend.schemas.inspection import LLMResultSchema
from backend.services import pipeline, prompts, scoring


async def score_chunk(
    session,
    assistant,
    chunk,
    evaluatee: str | None,
    client,
    cfg: dict | None,
    session_title: str | None = None,
    template_type: str | None = None,
) -> LLMResultSchema:
    """对一个 Chunk 完成一次完整质检主调用（L1/L2/L3 降级链），返回 guardrails 后的结果。"""
    template = scoring.load_template(session, template_type or assistant.template_type)
    rulebook = scoring.render_rulebook(template)
    system = prompts.build_system_prompt(rulebook)
    user = prompts.build_user_prompt(
        assistant.name,
        assistant.employee_no,
        template,
        chunk.numbered_text,
        session_title,
        assistant.teacher_persona,
        evaluatee,
        chunk.context_text,
    )
    result = await pipeline._call_main_with_fallbacks(
        client,
        system,
        user,
        chunk.numbered_text,
        session_title,
        cfg,
        assistant.teacher_persona,
        evaluatee,
    )
    return scoring.apply_guardrails(result, template, len(chunk.turns), max_turn=chunk.end_turn)
