# Result Aggregator：多 chunk 评分 → 汇总为最终评分（不简单平均）
# 单 chunk 直接采用（零额外 LLM 调用）；多 chunk 走汇总 Agent；汇总失败降级为规则合并（degraded=True）
# 汇总 prompt 全在本文件内，prompts.py 零改动；输出仍为 LLMResultSchema（现有契约零改动）
from statistics import mean

from backend.schemas.inspection import (
    D1Score,
    D2Score,
    D3Score,
    D4Score,
    DScores,
    HighlightItem,
    LLMResultSchema,
    S1Score,
    S1SubItems,
    S2Score,
    S2SubItems,
    S3Score,
    S3SubItems,
    SScores,
    SubItem,
)
from backend.services import scoring
from backend.services.batch import chunk_scorer
from backend.services.llm import json_guard
from backend.services.llm.base import LLMError

_SUMMARIZE_SYSTEM = """你是资深投顾会话质检专家，负责把同一员工的一次质检会话中多个切片的评分结果汇总为一份完整质检报告。

背景：
- 该员工的会话因内容过长被切分为多个切片，每个切片都已按统一《质检评分规则书》独立完成评分
  （含红灯、底层维度分、高亮与建议），各切片评分 JSON 中的轮次号均指向原会话的绝对轮次。
- 你的任务：基于全部切片评分，输出该员工本次会话的最终质检结果。

汇总口径：
1. 红灯一票否决：任一切片命中合规红线（承诺收益/保证回本/肯定涨跌、代客理财或替客户做决定、报具体买卖价格数字）
   → 最终 is_red_alert=true，red_alert_reasons 合并各切片原因 1~3 条。
2. 底层分汇总：每个底层分（D 各维度 score、S 各子项 score）取所有切片中该维度非 null 分的平均值（四舍五入取整）；
   不得简单取最高分切片或最低分切片的数值。
3. N/A 豁免：某维度仅当所有切片均为 null 时才输出 null + na_reason；只要任一切片可判定就必须给分。
4. highlight_dialogue：从各切片高亮中挑选扣分最严重的 ≤5 条；turn 必须使用原会话的绝对轮次号，original_text 必须逐字摘自原文。
5. improvement_suggestions：综合各切片给出 1~3 条个性化改进建议。
6. 算术剥离：你只负责底层分，不要计算汇总分数或总分（系统自动计算）。
7. 严格只输出一个 JSON 对象，不要输出任何其他文字、解释或 Markdown 代码块标记。"""


def _build_summarize_user(
    assistant_name: str, employee_no: str, chunks_results, session_title: str | None = None
) -> str:
    parts = [f"员工姓名：{assistant_name}", f"员工工号：{employee_no}"]
    if session_title:
        parts.append(f"会话标题：{session_title}")
    parts.append("")
    for i, (chunk, result) in enumerate(chunks_results, 1):
        parts.append(f"【切片 {i}（原会话轮次 {chunk.start_turn}-{chunk.end_turn}）】")
        parts.append("切片对话编号文本：")
        parts.append(chunk.numbered_text)
        parts.append("切片评分 JSON：")
        parts.append(result.model_dump_json())
        parts.append("")
    parts.append("请汇总输出最终质检 JSON。")
    return "\n".join(parts)


async def aggregate_chunks(
    session,
    chunks,
    assistant,
    evaluatee: str | None,
    client,
    cfg: dict | None,
    session_title: str | None = None,
    template_type: str | None = None,
) -> tuple[LLMResultSchema, bool]:
    """chunks → 最终评分。返回 (result, degraded)。

    - 单 chunk：逐 chunk 评分后直接采用，零额外 LLM 调用
    - 多 chunk：逐 chunk 评分 → 汇总 Agent；汇总 LLM 失败 → 规则合并降级（degraded=True）
    - 最终统一跑一次 apply_guardrails（算术剥离/熔断/红线补全在完整轮次上重算）
    """
    results = [
        await chunk_scorer.score_chunk(
            session, assistant, chunk, evaluatee, client, cfg, session_title, template_type
        )
        for chunk in chunks
    ]
    if len(chunks) == 1:
        merged, degraded = results[0], False
    else:
        try:
            merged = await json_guard.complete_json(
                client,
                _SUMMARIZE_SYSTEM,
                _build_summarize_user(assistant.name, assistant.employee_no, list(zip(chunks, results)), session_title),
                LLMResultSchema,
                retries=2,
                temperature=0.1,
            )
            degraded = False
        except LLMError:
            merged, degraded = _fallback_merge(results), True
    template = scoring.load_template(session, template_type or assistant.template_type)
    merged = scoring.apply_guardrails(
        merged,
        template,
        sum(len(c.turns) for c in chunks),
        max_turn=chunks[-1].end_turn,
    )
    return merged, degraded


# ---------- 降级：规则合并 ----------

def _first_nonempty(getter, results: list[LLMResultSchema]) -> str:
    for r in results:
        v = getter(r)
        if v:
            return v
    return ""


def _avg(vals) -> int | None:
    nums = [v for v in vals if v is not None]
    return round(mean(nums)) if nums else None


def _fallback_merge(results: list[LLMResultSchema]) -> LLMResultSchema:
    """规则合并：红灯任一；底层分非 null 均值；N/A 仅当全部 chunk 均 N/A；highlight 按 turn 去重取 5；建议频次取 3。"""
    def dedup(items):
        seen, out = set(), []
        for it in items:
            if it and it not in seen:
                seen.add(it)
                out.append(it)
        return out

    red = any(r.is_red_alert for r in results)
    red_reasons = dedup(r for rr in results for r in rr.red_alert_reasons)[:3] if red else []
    yellow_reasons = dedup(r for rr in results for r in rr.yellow_alert_reasons)[:3]

    def merge_d(dim, field_name):
        getter = lambda r: getattr(getattr(r.d_scores, dim), field_name)
        return _first_nonempty(getter, results)

    d1 = D1Score(
        analysis=merge_d("d1_emotion_change", "analysis"),
        score=_avg([r.d_scores.d1_emotion_change.score for r in results]),
        na_reason=merge_d("d1_emotion_change", "na_reason") or "所有切片均无法判定",
        rating=merge_d("d1_emotion_change", "rating"),
        comment=merge_d("d1_emotion_change", "comment"),
    )
    d2 = D2Score(
        analysis=merge_d("d2_profile_match", "analysis"),
        profile=merge_d("d2_profile_match", "profile"),
        score=_avg([r.d_scores.d2_profile_match.score for r in results]),
        na_reason=merge_d("d2_profile_match", "na_reason") or "所有切片均无法判定",
        match_rating=merge_d("d2_profile_match", "match_rating"),
        comment=merge_d("d2_profile_match", "comment"),
    )
    d3 = D3Score(
        analysis=merge_d("d3_problem_match", "analysis"),
        score=_avg([r.d_scores.d3_problem_match.score for r in results]),
        na_reason=merge_d("d3_problem_match", "na_reason") or "所有切片均无法判定",
        surface_vs_deep=merge_d("d3_problem_match", "surface_vs_deep"),
        resolution=merge_d("d3_problem_match", "resolution"),
        comment=merge_d("d3_problem_match", "comment"),
    )
    d4 = D4Score(
        analysis=merge_d("d4_expectation_exceed", "analysis"),
        score=_avg([r.d_scores.d4_expectation_exceed.score for r in results]),
        na_reason=merge_d("d4_expectation_exceed", "na_reason") or "所有切片均无法判定",
        derived_question=max((r.d_scores.d4_expectation_exceed.derived_question or 0) for r in results),
        control_given=max((r.d_scores.d4_expectation_exceed.control_given or 0) for r in results),
        comment=merge_d("d4_expectation_exceed", "comment"),
    )

    def merge_s(dim, sub_cls, sub_conf):
        """S 维度：子项均值；全 null → 维度 N/A。score 由 guardrails 重算（给 0 占位）。"""
        subs = {}
        for sub_key in sub_conf.model_fields:
            items = [getattr(getattr(r.s_scores, dim).sub_items, sub_key, None) for r in results]

            def analysis_of(k=sub_key):
                for r in results:
                    it = getattr(getattr(r.s_scores, dim).sub_items, k, None)
                    if it and it.analysis:
                        return it.analysis
                return ""

            subs[sub_key] = SubItem(analysis=analysis_of(), score=_avg([it.score if it else None for it in items]))
        all_null = all(sub.score is None for sub in subs.values())
        return sub_cls(
            score=None if all_null else 0,
            na_reason="" if not all_null else _first_nonempty(lambda r: getattr(r.s_scores, dim).na_reason, results) or "所有切片均无法判定",
            sub_items=sub_conf(**subs),
        )

    s1 = merge_s("s1_emotion_stabilize", S1Score, S1SubItems)
    s2 = merge_s("s2_problem_closure", S2Score, S2SubItems)
    s3 = merge_s("s3_professional_supply", S3Score, S3SubItems)

    # highlight 按 turn 去重（保留首现），按轮次排序取 5
    seen_turns = set()
    highlights: list[HighlightItem] = []
    for r in results:
        for h in r.highlight_dialogue:
            if h.turn in seen_turns:
                continue
            seen_turns.add(h.turn)
            highlights.append(h)
    highlights.sort(key=lambda h: h.turn)

    # 建议频次取 top 3
    from collections import Counter

    counts = Counter(s for r in results for s in r.improvement_suggestions if s and s.strip())
    suggestions = [s for s, _ in counts.most_common(3)]

    return LLMResultSchema(
        total_score=0,
        is_red_alert=red,
        red_alert_reasons=red_reasons,
        is_yellow_alert=False,
        yellow_alert_reasons=yellow_reasons,
        d_scores=DScores(
            d1_emotion_change=d1,
            d2_profile_match=d2,
            d3_problem_match=d3,
            d4_expectation_exceed=d4,
        ),
        s_scores=SScores(s1_emotion_stabilize=s1, s2_problem_closure=s2, s3_professional_supply=s3),
        highlight_dialogue=highlights[:5],
        improvement_suggestions=suggestions,
    )
