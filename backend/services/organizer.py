# 多人质检「自动识别并整理」：粘贴原始记录 → 一键生成【客户】【助理A】标签文本
# 识别完全复用 multiparser（三行式/块头式/marker 等格式），本模块只做 簇编号 + 序列化，
# 不引入任何新解析逻辑；内容逐字保留。
import string

from backend.services import multiparser
from backend.services.parser import ParseError
from backend.utils.errors import BizError


def organize_text(raw_text: str, assistant_db=None, name_map=None, not_assistant=None) -> dict:
    """解析原始聊天记录 → 整理为【客户】/【助理A】纯标签文本。

    输入为任意 multiparser 可识别格式（用户真实格式为三行式：发送人/时间戳/内容）；
    输出逐条消息一行：【客户】内容 / 【助理{字母}】内容（多行内容保留原样换行，
    重新进质检解析时由 multiparser 内容续行机制并回）。助理按消息首现顺序编号 A/B/C…，
    同一簇（同显示名/员工）恒为同一字母。解析失败（空/不可识别）→ BizError 400。
    """
    try:
        result = multiparser.parse_multi(raw_text, assistant_db, name_map, not_assistant)
    except ParseError as exc:
        raise BizError("organize_failed", str(exc), status_code=400) from exc
    # 簇 → 字母：按簇内消息首次出现（最小 turn_no）排序分配 A/B/C…
    ordered = sorted(
        result.clusters,
        key=lambda c: min(c.reply_turn_nos) if c.reply_turn_nos else 10**9,
    )
    letters = []
    for i, c in enumerate(ordered):
        letters.append(string.ascii_uppercase[i] if i < len(string.ascii_uppercase) else "?")
    turn_letter = {}
    for c, letter in zip(ordered, letters):
        for t in c.reply_turn_nos:
            turn_letter[t] = letter
    lines = []
    for m in result.messages:
        label = f"助理{turn_letter.get(m.turn_no, '?')}" if m.role == "助" else "客户"
        lines.append(f"【{label}】{m.text}")
    return {
        "organized_text": "\n".join(lines),
        "message_count": len(result.messages),
        "role_stats": result.role_stats,
        "assistants": [
            {"canonical_name": c.canonical_name, "label": f"助理{letter}"}
            for c, letter in zip(ordered, letters)
        ],
        "warnings": result.warnings,
    }
