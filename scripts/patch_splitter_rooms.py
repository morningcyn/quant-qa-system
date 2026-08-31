# 一次性补丁：splitter.py 增加 rooms 模式（_split_rooms + split_customers 签名扩展）
import io

P = "backend/services/batch/splitter.py"
src = io.open(P, encoding="utf-8").read()

old = '''def split_customers(raw_text: str, assistant_db=None, name_map=None, not_assistant=None) -> dict:
    """整批文本 → 客户会话列表。

    返回 {customers, warnings, task_count, message_count, assistant_count, parse_error}
    - 客侧 speaker（昵称）变化 = 新会话起点；会话内跨助轮不切
    - 无客侧消息 / 纯 marker 格式（客户恒为「客户」）→ 整段单任务 + warning
    - parse_multi 抛 ParseError → 不 400：建 1 个 task，执行时失败显示解析错误（导入总是成功）
    """
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
        }'''

new = '''def split_customers(raw_text: str, assistant_db=None, name_map=None, not_assistant=None, rooms=None) -> dict:
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
        }'''

n = src.count(old)
assert n == 1, f"匹配数: {n}"
src = src.replace(old, new)

# 文件末尾追加 _split_rooms
extra = '''


def _split_rooms(rooms, assistant_db=None, name_map=None, not_assistant=None) -> dict:
    """Excel「房间对话」导出 → 每个房间一个客户会话。

    每个房间的完整聊天记录经 normalize_room_text 归一化为系统可解析的
    「时间戳 发送人\\n内容」格式，逐条调用 multiparser 解析；解析失败的房间跳过
    并记 warning（不影响其他房间）。
    """
    customers: list[CustomerSession] = []
    warnings: list[str] = []
    parse_error: str | None = None
    assistant_names_all: list[str] = []

    for i, room in enumerate(rooms, start=1):
        cid = f"c{i:04d}"
        name = (room.customer_name or "").strip() or f"客户{i}"
        try:
            result = multiparser.parse_multi(
                normalize_room_text(room.text), assistant_db, name_map, not_assistant
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
'''
src = src + extra

io.open(P, "w", encoding="utf-8").write(src)
print("splitter.py rooms 模式补丁完成")
