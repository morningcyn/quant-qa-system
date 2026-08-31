# 客户情绪分析器：消息重建（双锚点）/ LLM 分批调用 / 确定性兜底 / 落库
import json
import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from backend.db import batch_repository as brepo
from backend.db import repository
from backend.services import multiparser
from backend.services.emotion.derive import EMOTION_SCORE, build_curve, build_summary
from backend.services.emotion.prompts import build_emotion_system_prompt, build_emotion_user_prompt
from backend.services.emotion.schemas import EmotionResult
from backend.services.llm import json_guard
from backend.utils.errors import BizError

logger = logging.getLogger(__name__)

BATCH_SIZE = 40          # 每批客户消息数（输出预算 < max_tokens 4096）
BATCH_OVERLAP = 3        # 相邻批重叠条数（辅助语境；合并时后批覆盖）
CHAR_BUDGET = 20000      # 单批输入字数上限（触发分批的另一个条件）
MAX_TOKENS = 4096

# 多人质检降级重建（无总览时）：[turn_no][speaker] text（to_numbered_text 产物）
_NUMBERED_LINE_RE = re.compile(r"^\[(\d+)\]\[([^\]]+)\]\s?(.*)$")


async def analyze_session(
    session: Session,
    msgs: list,
    title: str | None,
    conversation_id: str,
    source_type: str,
    customer_name: str | None,
    client,
    cfg: dict,
    warning: str | None = None,
) -> Any | None:
    """对一个客户会话做情绪分析并落库（幂等 upsert）。

    msgs 为 MultiMessage 列表（客/助有序，助轮已带 assistant_id）。
    返回 EmotionSession；会话无客户消息 → None（静默跳过）。
    任何 LLM 批次失败 → 上抛，由调用方决定降级（批量只记日志，不影响任务状态）。
    """
    cust = [m for m in msgs if getattr(m, "role", None) == "客"]
    if not cust:
        return None

    # 有文本的客轮进 LLM；空文本客轮（解析产物允许空）确定性合成，不浪费 token
    llm_input = [(m.turn_no, m.text) for m in cust if (m.text or "").strip()]
    synthesized: dict[int, dict] = {
        m.turn_no: {
            "turn_no": m.turn_no,
            "emotion": "中性",
            "intensity": 0,
            "confidence": 0.0,
            "trigger": "其他",
            "evidence": "",
            "synthesized": True,
            "evidence_adjusted": False,
        }
        for m in cust
        if not (m.text or "").strip()
    }
    text_by_turn = {m.turn_no: m.text for m in cust if (m.text or "").strip()}

    items: dict[int, dict] = {}
    if llm_input:
        for batch in _chunk_inputs(llm_input):
            result = await json_guard.complete_json(
                client,
                build_emotion_system_prompt(),
                build_emotion_user_prompt(batch),
                EmotionResult,
                retries=2,
                temperature=0.1,
                max_tokens=MAX_TOKENS,
            )
            for it in result.items:
                if it.turn_no in text_by_turn:
                    items[it.turn_no] = _finalize_item(it.model_dump(), text_by_turn[it.turn_no])
    items.update(synthesized)

    # LLM 漏标 → 合成中性（confidence=0.0 自然落入低置信度标记）
    for turn_no in text_by_turn:
        if turn_no not in items:
            items[turn_no] = {
                "turn_no": turn_no,
                "emotion": "中性",
                "intensity": 0,
                "confidence": 0.0,
                "trigger": "其他",
                "evidence": "",
                "synthesized": True,
                "evidence_adjusted": False,
            }

    ordered = [items[m.turn_no] for m in cust if m.turn_no in items]
    # 组装后统一补算 emotion_score（emotion 的确定性映射，不进 LLM 契约；合成中性=0）
    for it in ordered:
        it["emotion_score"] = EMOTION_SCORE[it["emotion"]]
    summary = build_summary(msgs, ordered)
    # 快照改全量消息（客+助），保留时间戳与助理绑定 —— 情绪曲线的时间轴与助理回复节点依赖它
    message_snapshot = [
        {
            "turn_no": m.turn_no,
            "role": getattr(m, "role", "客"),
            "speaker": m.speaker,
            "canonical_name": getattr(m, "canonical_name", None),
            "text": m.text,
            "timestamp": getattr(m, "timestamp", None),
            "assistant_id": getattr(m, "assistant_id", None),
        }
        for m in msgs
    ]
    summary["curve"] = build_curve(message_snapshot, ordered)
    return repository.save_emotion_session(
        session,
        conversation_id=conversation_id,
        source_type=source_type,
        title=title,
        customer_name=customer_name,
        items=ordered,
        summary=summary,
        messages=message_snapshot,
        degraded=bool(warning),
        warning=warning,
    )


def _chunk_inputs(lines: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    """按条数与字数分批；相邻批重叠 BATCH_OVERLAP 条（后批基于更多上下文，合并时后批覆盖）。"""
    batches: list[list[tuple[int, str]]] = []
    cur: list[tuple[int, str]] = []
    cur_chars = 0
    for line in lines:
        if cur and (len(cur) >= BATCH_SIZE or cur_chars + len(line[1]) > CHAR_BUDGET):
            batches.append(cur)
            cur = []
            cur_chars = 0
        cur.append(line)
        cur_chars += len(line[1])
    if cur:
        batches.append(cur)
    if len(batches) <= 1:
        return batches
    # 重叠：后批头部并入前批尾部 BATCH_OVERLAP 条
    overlapped = [batches[0]]
    for b in batches[1:]:
        prev = overlapped[-1]
        overlap = prev[-BATCH_OVERLAP:] if len(prev) >= BATCH_OVERLAP else prev
        overlapped.append(overlap + b)
    return overlapped


def _finalize_item(item: dict, text: str) -> dict:
    """机器级 evidence 逐字校验：未逐字命中原文 → 替换为全文（绝不改写），并置标记。"""
    evidence = (item.get("evidence") or "").strip()
    if evidence and evidence in text:
        item["evidence"] = evidence  # 命中则写入去除首尾空白后的原文片段
        item["evidence_adjusted"] = False
    else:
        item["evidence"] = text
        item["evidence_adjusted"] = True
    item["synthesized"] = False
    return item


# ---------- 报告 → 会话上下文解析（双锚点） ----------

def resolve_inspection_context(session: Session, inspection, need_messages: bool = True) -> dict:
    """由一份质检报告定位客户会话并重建消息序列。

    返回 {conversation_id, source_type, msgs, title, customer_name}。
    无法定位（单助理无会话数据 / 批量老数据）→ 抛 BizError("emotion_unsupported", ...)。
    """
    cid = inspection.conversation_id
    if not cid:
        raise BizError("emotion_unsupported", "该报告无会话级数据（单助理评分生成，无法做会话级情绪分析）", status_code=404)
    if brepo.get_batch(session, cid) is not None:
        return _resolve_batch(session, inspection, cid, need_messages)
    return _resolve_multi(session, inspection, cid, need_messages)


def _resolve_batch(session: Session, inspection, batch_id: str, need_messages: bool) -> dict:
    """批量模式：conversation_id=batch_id → 反查所属任务 → 情绪锚点 batch_id:task_id。"""
    for t in brepo.list_tasks(session, batch_id):
        result = json.loads(t.result_json) if t.result_json else None
        ids = [r["inspection_id"] for r in (result or {}).get("reports") or []]
        if inspection.id in ids:
            if not need_messages:
                return {
                    "conversation_id": f"{batch_id}:{t.task_id}",
                    "source_type": "batch",
                    "msgs": [],
                    "title": None,
                    "customer_name": t.customer_name,
                }
            data = json.loads(t.input_data) if t.input_data else {}
            from backend.services.batch.splitter import dict_to_message

            msgs = [dict_to_message(m) for m in data.get("messages") or []]
            return {
                "conversation_id": f"{batch_id}:{t.task_id}",
                "source_type": "batch",
                "msgs": msgs,
                "title": data.get("title"),
                "customer_name": t.customer_name,
            }
    raise BizError("emotion_unsupported", "无法定位所属批量任务（老数据），暂不支持情绪分析", status_code=404)


def _resolve_multi(session: Session, inspection, conversation_id: str, need_messages: bool) -> dict:
    """多人质检模式：锚点=conversation_id；优先用总览原始记录重建，无总览降级合并兄弟报告。"""
    if not need_messages:
        return {
            "conversation_id": conversation_id,
            "source_type": "multi",
            "msgs": [],
            "title": inspection.session_title,
            "customer_name": None,
        }
    ov = repository.get_overview_by_conversation(session, conversation_id)
    warning = None
    if ov is not None:
        result = multiparser.parse_multi(
            ov.raw_dialogue,
            repository.list_assistants(session),
            multiparser.load_name_map(),
            multiparser.load_not_assistant_names(),
        )
        msgs = list(result.messages)
        title = ov.title
    else:
        msgs, warning = _rebuild_from_siblings(session, inspection, conversation_id)
        title = inspection.session_title
    customer_name = None
    for m in msgs:
        if getattr(m, "role", None) == "客" and m.speaker and m.speaker != "客户":
            customer_name = m.speaker
            break
    return {
        "conversation_id": conversation_id,
        "source_type": "multi",
        "msgs": msgs,
        "title": title,
        "customer_name": customer_name,
        "warning": warning,
    }


def _rebuild_from_siblings(session: Session, inspection, conversation_id: str) -> tuple[list, str | None]:
    """降级重建：合并同会话全部兄弟报告 raw_dialogue（编号文本），按 turn_no 去重排序。"""
    siblings = repository.list_inspections_by_conversation(session, conversation_id)
    turns: dict[int, tuple[str, str]] = {}  # turn_no -> (speaker, text)
    for sib in siblings:
        if sib.detail is None:
            continue
        cur_no, cur_spk = None, None
        for line in sib.detail.raw_dialogue.splitlines():
            m = _NUMBERED_LINE_RE.match(line.strip())
            if m:
                cur_no = int(m.group(1))
                cur_spk = m.group(2)
                turns.setdefault(cur_no, (cur_spk, m.group(3)))
            elif cur_no is not None:
                spk, text = turns[cur_no]
                turns[cur_no] = (spk, f"{text}\n{line}")

    msgs = []
    for no in sorted(turns):
        spk, text = turns[no]
        role = "助" if spk and spk != "客户" else "客"
        msgs.append(
            multiparser.MultiMessage(
                turn_no=no,
                role=role,
                speaker=spk or "",
                canonical_name=spk if role == "助" else "客户",
                text=text,
                timestamp=None,
                assistant_id=None,
                raw_line="",
            )
        )
    warning = None
    if msgs:
        # 尾部补一轮客户消息的极端截断场景仅提示，不做数据修补
        warning = "总览缺失，情绪分析由各报告对话记录重建（助理归属可能不完整）"
    return msgs, warning
