# 批量会话切分：整批聊天记录 → 按「客户昵称变化」切分为多个客户会话（纯规则，不调 LLM）
# 一个客户会话 = 一个 BatchTask；同客户多位助理轮替保留在同一会话内
# rooms 模式：Excel「房间对话」导出（每行一个房间的完整聊天记录）→ 每个房间独立会话
import re
from dataclasses import dataclass, field

from backend.services import multiparser
from backend.services.parser import ParseError

# 房间导出发送人行：【时间】发送人（角色标注）——「曹*（客户）」「曹瑞格（投顾助理；…）」
# 系统导出文本解析只支持裸时间戳（无【】）+ ≤24 字符发送人，此处归一化为「时间 名字」
_ROOM_SENDER_RE = re.compile(r"^【(?P<ts>[^】]+)】\s*(?P<body>.+)$")


def normalize_room_text(text: str) -> str:
    """房间完整聊天记录 → 系统可解析的导出文本格式。

    「【2026-08-19 17:35:44】曹*（客户）」→「2026-08-19 17:35:44 曹*」
    名字取「（」前部分；角色不显式保留（客/助由发送人判定：客户昵称→客、中文助理名→助）。
    """
    out: list[str] = []
    for raw in text.splitlines():
        m = _ROOM_SENDER_RE.match(raw.strip())
        if m:
            name = m.group("body").split("（")[0].strip()
            out.append(f"{m.group('ts')} {name}")
        else:
            out.append(raw)
    return "\n".join(out)


@dataclass
class CustomerSession:
    customer_id: str        # 批内 c0001…
    customer_name: str      # 首个客侧 speaker（昵称）
    messages: list          # MultiMessage 列表（原样保留，input_data 事实源）
    message_count: int = 0
    assistant_names: list = field(default_factory=list)  # 参与助理（去重，按出现顺序）


def message_to_dict(m: multiparser.MultiMessage) -> dict:
    return {
        "turn_no": m.turn_no,
        "role": m.role,
        "speaker": m.speaker,
        "canonical_name": m.canonical_name,
        "text": m.text,
        "timestamp": m.timestamp,
        "assistant_id": m.assistant_id,
        "raw_line": m.raw_line,
    }


def dict_to_message(d: dict) -> multiparser.MultiMessage:
    return multiparser.MultiMessage(
        turn_no=d["turn_no"],
        role=d["role"],
        speaker=d["speaker"],
        canonical_name=d["canonical_name"],
        text=d["text"],
        timestamp=d.get("timestamp"),
        assistant_id=d.get("assistant_id"),
        raw_line=d.get("raw_line", ""),
    )


def split_customers(raw_text: str, assistant_db=None, name_map=None, not_assistant=None, rooms=None) -> dict:
    """整批文本 / 逐房间记录 → 客户会话列表。

    返回 {customers, warnings, task_count, message_count, assistant_count, parse_error}
    - raw_text 模式：客侧 speaker（昵称）变化 = 新会话起点；会话内跨助轮不切
    - rooms 模式（Excel 房间导出）：每个房间独立会话，记录归一化后逐条解析
    - 无客侧消息 / 纯 marker 格式（客户恒为「客户」）→ 整段单任务 + warning
    - parse_multi 抛 ParseError → 不 400：建 1 个 task，执行时失败显示解析错误（导入总是成功）
    """
    if rooms:
        return _split_rooms(rooms, assistant_db, name_map, not_assistant)
    try:
        result = multiparser.parse_multi(raw_text, assistant_db, name_map, not_assistant)
    except ParseError as exc:
        return {
            "customers": [],
            "warnings": [str(exc)],
            "task_count": 1,
            "message_count": 0,
            "assistant_count": 0,
            "parse_error": str(exc),
        }

    customers: list[CustomerSession] = []
    current: CustomerSession | None = None
    current_customer_name: str | None = None
    pending_assistant_names: list[str] = []

    for m in result.messages:
        if m.role == "客":
            # 客侧昵称变化 → 新会话起点
            if current_customer_name != m.speaker:
                current_customer_name = m.speaker
                current = CustomerSession(
                    customer_id=f"c{len(customers) + 1:04d}",
                    customer_name=m.speaker or "客户",
                    messages=[],
                    assistant_names=list(pending_assistant_names),  # 会话前暂存的助理名并入
                )
                pending_assistant_names = []
                customers.append(current)
            if current is not None:
                current.messages.append(m)
                current.message_count += 1
        else:
            # 助理消息归属当前会话；开头无客户消息时暂存，等第一个客户会话建立后并入
            if current is not None:
                current.messages.append(m)
                current.message_count += 1
                if m.canonical_name not in current.assistant_names:
                    current.assistant_names.append(m.canonical_name)
            elif m.canonical_name not in pending_assistant_names:
                pending_assistant_names.append(m.canonical_name)

    warnings = list(result.warnings)
    if not customers:
        # 兜底：无客侧消息（全是助理轮）→ 整段单任务
        customers.append(
            CustomerSession(
                customer_id="c0001",
                customer_name="客户",
                messages=list(result.messages),
                message_count=len(result.messages),
                assistant_names=[c.canonical_name for c in result.clusters],
            )
        )
        warnings.append("未识别到客户消息，整段作为单任务处理")
    cust_msgs = sum(1 for c in customers for m in c.messages if m.role == "客")
    if len(customers) == 1 and cust_msgs > 1:
        # 客户昵称恒同（如纯 marker 格式或单个客户）→ 整段单任务（正常提示，非错误）
        warnings.append("未按客户昵称切分出多个会话（客户昵称相同或缺失），整段作为单任务处理")

    assistant_names = list(dict.fromkeys(c.canonical_name for c in result.clusters))
    return {
        "customers": customers,
        "warnings": warnings,
        "task_count": len(customers),
        "message_count": sum(c.message_count for c in customers),
        "assistant_count": len(assistant_names),
        "parse_error": None,
    }



def _split_rooms(rooms, assistant_db=None, name_map=None, not_assistant=None) -> dict:
    """Excel「房间对话」导出 → 每个房间一个客户会话。

    每个房间的完整聊天记录经 normalize_room_text 归一化为系统可解析的
    「时间戳 发送人\n内容」格式，逐条调用 multiparser 解析；解析失败的房间跳过
    并记 warning（不影响其他房间）。
    """
    customers: list[CustomerSession] = []
    warnings: list[str] = []
    parse_error: str | None = None
    assistant_names_all: list[str] = []

    for i, room in enumerate(rooms, start=1):
        cid = f"c{i:04d}"
        room_name = room.get("customer_name") if isinstance(room, dict) else getattr(room, "customer_name", None)
        room_text = room.get("text") if isinstance(room, dict) else getattr(room, "text", "")
        name = (room_name or "").strip() or f"客户{i}"
        try:
            result = multiparser.parse_multi(
                normalize_room_text(room_text), assistant_db, name_map, not_assistant
            )
        except ParseError as exc:
            warnings.append(f"房间 {name} 解析失败，已跳过：{exc}")
            parse_error = parse_error or str(exc)
            continue

        msgs = list(result.messages)
        # 房间内无客户消息 → 跳过（没有评分对象）
        if not any(m.role == "客" for m in msgs):
            warnings.append(f"房间 {name} 无客户消息，已跳过")
            continue
        customers.append(
            CustomerSession(
                customer_id=cid,
                customer_name=name,
                messages=msgs,
                message_count=len(msgs),
                assistant_names=[c.canonical_name for c in result.clusters],
            )
        )
        for c in result.clusters:
            if c.canonical_name not in assistant_names_all:
                assistant_names_all.append(c.canonical_name)
        warnings.extend(result.warnings)

    if not customers:
        warnings.append("未识别到任何可评分的客户会话")
    return {
        "customers": customers,
        "warnings": warnings,
        "task_count": len(customers),
        "message_count": sum(c.message_count for c in customers),
        "assistant_count": len(assistant_names_all),
        "parse_error": parse_error,
    }
