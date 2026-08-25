# 会话结构化解析：marker 文本 / CSV / JSON 三格式 + 防呆校验（纯正则，不调 LLM）
import csv
import io
import json
import re
from dataclasses import dataclass, field

from backend.utils.errors import BizError

# 客户侧角色词 → "客"；助理侧角色词 → "助"
_CUSTOMER_WORDS = ("客", "客户", "客服", "顾客", "用户", "customer", "user", "K")
_ASSISTANT_WORDS = ("助", "助手", "助理", "理财师", "投顾", "顾问", "老师", "assistant", "agent", "A")

_ROLE_WORDS = _CUSTOMER_WORDS + _ASSISTANT_WORDS
# 长词在前，避免 "客户" 被 "客" 抢先匹配
_ROLE_ALT = "|".join(sorted(set(_ROLE_WORDS), key=len, reverse=True))

# 行首角色标记：支持 [客] / 【客】 / (客) / 客： / 客: / "客" 等写法
_MARKER_RE = re.compile(
    rf"^\s*(?:[\[【(（\"'']\s*)?(?P<role>{_ROLE_ALT})\s*(?:[\]】)）\"'']\s*|[:：]\s*)?(?P<text>.*)$",
    re.IGNORECASE,
)
# 单独字母标记（K:/A:）必须带冒号，避免误伤普通行
_ABBR_RE = re.compile(r"^\s*(?P<role>[Kk]|A)[:：]\s*(?P<text>.*)$")

_ROLE_MAP = {w: "客" for w in _CUSTOMER_WORDS} | {w: "助" for w in _ASSISTANT_WORDS}

MAX_TURN_CHARS = 2000
MAX_TOTAL_CHARS = 40000
MIN_TURNS_WARN = 4
MAX_SAME_ROLE_RUN = 10

# CSV 表头别名
_ROLE_HEADERS = {"角色", "发言人", "说话人", "身份", "role", "speaker", "who"}
_TEXT_HEADERS = {"内容", "消息", "对话", "正文", "content", "text", "message", "msg"}


@dataclass
class Turn:
    role: str  # "客" | "助"
    text: str
    turn_no: int


@dataclass
class ParseResult:
    turns: list[Turn] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    role_stats: dict = field(default_factory=dict)
    fmt: str = "text"


class ParseError(BizError):
    def __init__(self, message: str):
        super().__init__("parse_failed", message, status_code=400)


def normalize_role(raw: str) -> str | None:
    word = raw.strip().lower()
    for key, value in _ROLE_MAP.items():
        if word == key.lower():
            return value
    return None


def parse_raw(raw: str) -> ParseResult:
    text = (raw or "").strip()
    if not text:
        raise ParseError("内容为空，请粘贴或上传会话记录")
    if _looks_like_json(text):
        return _parse_json(text)
    if _looks_like_csv(text):
        return _parse_csv(text)
    result = _parse_marker_text(text)
    if not result.turns:
        raise ParseError(
            "未能识别对话角色与轮次。请确认文本包含 [客]/[助]、客：/助： 等角色标记，"
            "或改用 JSON/CSV 格式（两列：角色、内容）导入"
        )
    return result


# ---------- 格式嗅探 ----------

def _looks_like_json(text: str) -> bool:
    if not text.lstrip().startswith("["):
        return False
    try:
        return isinstance(json.loads(text), list)
    except json.JSONDecodeError:
        return False


def _looks_like_csv(text: str) -> bool:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    if not first_line:
        return False
    lower = first_line.lower()
    if "," not in first_line and "\t" not in first_line:
        return False
    if any(h in lower for h in _ROLE_HEADERS | _TEXT_HEADERS):
        return True
    return False


# ---------- marker 文本 ----------

def _parse_marker_text(text: str) -> ParseResult:
    turns: list[Turn] = []
    preamble_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _MARKER_RE.match(line)
        abbr = _ABBR_RE.match(line)
        role_word = None
        content = None
        if abbr:
            role_word, content = abbr.group("role"), abbr.group("text")
        elif match and match.group("role"):
            role_word, content = match.group("role"), match.group("text")
        if role_word and normalize_role(role_word):
            turns.append(Turn(role=normalize_role(role_word), text=content.strip(), turn_no=len(turns) + 1))
            continue
        if turns:
            turns[-1].text = f"{turns[-1].text}\n{line}".strip()  # 换行续写并入上一轮
        else:
            preamble_lines.append(line)
    result = ParseResult(turns=turns, fmt="text")
    if preamble_lines and sum(len(x) for x in preamble_lines) > 50:
        result.warnings.append("开头存在未被识别为对话的文本（可能是导出文件的说明信息），已忽略")
    _finalize(result, text)
    return result


# ---------- CSV ----------

def _parse_csv(text: str) -> ParseResult:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise ParseError("CSV 内容为空")
    header = [c.strip().lower() for c in rows[0]]
    role_idx = text_idx = None
    for i, cell in enumerate(header):
        if cell in _ROLE_HEADERS:
            role_idx = i
        elif cell in _TEXT_HEADERS:
            text_idx = i
    if role_idx is None or text_idx is None:
        if len(header) >= 2 and not any(h in _ROLE_HEADERS | _TEXT_HEADERS for h in header):
            role_idx, text_idx = 0, 1  # 无表头两列：按位置兜底
        else:
            raise ParseError("CSV 缺少可识别表头：需要「角色」「内容」两列（或 speaker/content 等）")
    turns: list[Turn] = []
    skipped = 0
    for row in rows[1:]:
        if len(row) <= max(role_idx, text_idx):
            skipped += 1
            continue
        role = normalize_role(row[role_idx])
        if role is None:
            skipped += 1
            continue
        content = row[text_idx].strip()
        if not content:
            skipped += 1
            continue
        turns.append(Turn(role=role, text=content, turn_no=len(turns) + 1))
    result = ParseResult(turns=turns, fmt="csv")
    if skipped:
        result.warnings.append(f"{skipped} 行因角色无法识别或内容为空被跳过")
    if not turns:
        raise ParseError("CSV 中没有可识别的对话行（角色列需为 客/助 等）")
    _finalize(result, text)
    return result


# ---------- JSON ----------

def _parse_json(text: str) -> ParseResult:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"JSON 解析失败：{exc}") from exc
    if not isinstance(data, list) or not data:
        raise ParseError("JSON 需为非空数组，例如 [{\"role\": \"客\", \"content\": \"...\"}]")
    turns: list[Turn] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ParseError(f"JSON 第 {i + 1} 项不是对象")
        role_raw = item.get("role") or item.get("speaker") or item.get("角色") or item.get("发言人")
        text_raw = item.get("content") or item.get("text") or item.get("message") or item.get("内容")
        if role_raw is None or text_raw is None:
            raise ParseError(f"JSON 第 {i + 1} 项缺少 role/content 字段")
        role = normalize_role(str(role_raw))
        if role is None:
            raise ParseError(f"JSON 第 {i + 1} 项角色「{role_raw}」无法识别（支持 客/助/客户/customer/assistant 等）")
        content = str(text_raw).strip()
        if content:
            turns.append(Turn(role=role, text=content, turn_no=len(turns) + 1))
    result = ParseResult(turns=turns, fmt="json")
    if not turns:
        raise ParseError("JSON 中没有可识别的对话内容")
    _finalize(result, text)
    return result


# ---------- 防呆校验与规范化 ----------

def _finalize(result: ParseResult, raw_text: str) -> None:
    turns = result.turns
    result.role_stats = {
        "客": sum(1 for t in turns if t.role == "客"),
        "助": sum(1 for t in turns if t.role == "助"),
        "total": len(turns),
    }
    if len(turns) < MIN_TURNS_WARN:
        result.warnings.append(
            f"对话轮数较少（{len(turns)} 轮），可能影响评分准确性，建议导入完整会话"
        )
    if result.role_stats["客"] == 0:
        result.warnings.append("未识别到客户发言，请检查格式（D2 画像匹配等维度将难以评分）")
    if result.role_stats["助"] == 0:
        result.warnings.append("未识别到助理发言，请检查格式")
    total_chars = sum(len(t.text) for t in turns)
    if total_chars > MAX_TOTAL_CHARS:
        result.warnings.append("对话总长度超过 40000 字，模型可能截断，建议拆分后再质检")
    for t in turns:
        if len(t.text) > MAX_TURN_CHARS:
            result.warnings.append(f"第 {t.turn_no} 轮单轮超过 2000 字，可能影响解析质量")
    run = 1
    for prev, cur in zip(turns, turns[1:]):
        if cur.role == prev.role:
            run += 1
        else:
            run = 1
        if run >= MAX_SAME_ROLE_RUN:
            result.warnings.append(
                f"第 {cur.turn_no} 轮附近连续 {run} 轮同一角色，疑似角色标记缺失或格式错位"
            )
            break


def to_numbered_text(turns: list[Turn], max_chars: int | None = None) -> str:
    """生成送模型的规范文本 [1][客] ...，保证 highlight 的 turn 可对齐。"""
    lines = [f"[{t.turn_no}][{t.role}] {t.text}" for t in turns]
    text = "\n".join(lines)
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + "\n...(内容过长已截断)"
    return text


def summarize_long_dialogue(turns: list[Turn], keep_head: int = 10, keep_tail: int = 10) -> str:
    """超长对话（>60 轮）：头尾保留全文，中间轮次按规则压缩为逐轮一句话摘要（简单截断版）。"""
    if len(turns) <= 60:
        return to_numbered_text(turns)
    head = turns[:keep_head]
    tail = turns[-keep_tail:]
    mid_lines = []
    for t in turns[keep_head:-keep_tail]:
        summary = t.text[:60].replace("\n", " ") + ("…" if len(t.text) > 60 else "")
        mid_lines.append(f"[{t.turn_no}][{t.role}]（摘要）{summary}")
    return "\n".join(
        [to_numbered_text(head)] + mid_lines + [to_numbered_text(tail)]
    ) + "\n（注：中间轮次已压缩为摘要，请主要依据头尾全文评分）"
