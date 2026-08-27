# 多人助理聊天记录解析（上游模块）：完整会话 → 结构化消息 → 助理识别/归并 → 员工匹配 → 服务分段
# 纯确定性规则，不调 LLM；与单助理评分模块（pipeline.run_inspection）解耦，只产出评分素材。
import csv
import io
import json
import re
from dataclasses import dataclass, field

from backend.services.parser import (
    ParseError,
    Turn,
    match_marker,
    normalize_role,
    summarize_long_dialogue,
    to_numbered_text,
)

# 时间戳：行首日期时间，支持带年份（时间可选）、无年份（必须带时间，防"10.25 补仓提醒"内容行误判）、
# 纯时间、毫秒（.123）、T 分隔（2026-08-24T10:23:45）
_TIMESTAMP_RE = re.compile(
    r"^\s*("
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?)?|"
    r"\d{1,2}[-/.]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?|"
    r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?"
    r")\s*"
)
# 整行时间戳（说话人与内容之间的独立时间戳行）
_TS_FULL_RE = re.compile(
    r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?)?$"
    r"|^\d{1,2}[-/.]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?$"
    r"|^\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?$"
)
# 块头声明行：客户|助理|客服 + 昵称/姓名（冒号可选，尾部可粘连时间戳）
_HEADER_LINE_RE = re.compile(r"^(客户|助理|客服|投顾|顾问)\s*[：:]?\s*(.+)$")
# 名字/昵称行（行首时间戳已剥离）："哈尔滨赢家1122" / "韩珂龙头班（胡馨月）"
_NAME_LINE_RE = re.compile(r"^(?P<name>[^：:]{1,24})$")
# 严格发送人行（三行式导出"时间戳行/发送人行/内容行"用）：排除常见标点避免吞内容行，
# 但允许括号（班级名（姓名）格式）
_SENDER_LINE_RE = re.compile(
    r"^(?P<name>[^：:，,。.！!？?、；;…“”‘’\"'%<>/\\|]{1,24})$"
)
# 未知说话人行："名字：内容"
_UNKNOWN_LINE_RE = re.compile(r"^(?P<name>[^：:\s]{1,16})\s*[:：]\s*(?P<text>.+)$")
# 方括号裸名说话人："[名字] 内容"（非角色词；客户通常用 [客] 或块头格式，故视为助理候选）
_BRACKET_NAME_RE = re.compile(r"^[\[【(（]\s*(?P<name>[^\]】)）：:\s]{1,24})\s*[\]】)）]\s*(?P<text>.+)$")

# 自动角色推断用词（发送人含这些词素 → 助；刻意排除"老师"：客户也可能是"王老师"）
_AUTO_ASSISTANT_WORDS = (
    "客服", "助理", "投顾", "顾问", "座席", "理财师", "人工", "热线", "agent", "assistant",
)
# 称谓后缀（匹配/归并用；发送人含这些后缀 → 客："李姐/张哥/王经理/王老师/张先生/李女士"）
_HONORIFIC_RE = re.compile(r"(老师|先生|女士|小姐|姐|哥|经理|顾问)$")
# 尾部标识符（客服A / 投顾2 / 助理3 → A / 2 / 3，用于同人异名归并）
_IDENT_TAIL_RE = re.compile(r"[A-Za-z0-9一二三四五六七八九十百千]+$")
# 前缀式角色值（CSV 发送人：客服张三 / 客户王先生）
_ASSISTANT_PREFIX_RE = re.compile(r"^(客服|投顾|助理|顾问|老师|理财师|座席|运营|班主任|管理员)[（(]?[^（()）]{1,16}[）)]?$")
_CUSTOMER_PREFIX_RE = re.compile(r"^(客户|会员|用户|顾客|学员|投资者|投资用户)[（(]?[^（()）]{1,16}[）)]?$")
# 短回应词（"好的/嗯/收到/OK"等）：三行式/微信双行中独立成行的短回应是客户回复内容，
# 不得被误判为发送人行（否则角色颠倒+内容错位）
_SHORT_ACK_RE = re.compile(
    r"^(好的?|嗯{1,4}|可以(的|了)?|收到(了)?|谢谢(了)?|再见|拜拜|好|行|对|是|哦|知道了?|了解|明白|没问题|OK|ok|Ok|yes|Yes|no|No)$"
)
_ALNUM_ONLY_RE = re.compile(r"^[0-9A-Za-z]+$")


def _is_short_acknowledgment(s: str) -> bool:
    """短回应判定：短回应词表，或 ≤6 位纯字母数字（"666"/"okay"）。"""
    t = s.strip()
    return bool(_SHORT_ACK_RE.fullmatch(t)) or (len(t) <= 6 and bool(_ALNUM_ONLY_RE.fullmatch(t)))

# CSV/JSON 列名
_TIME_HEADERS = {"时间", "日期", "time", "date", "timestamp", "datetime"}

# 上下文（仅参考不计分）上限
MAX_CONTEXT_TURNS = 10
MAX_CONTEXT_CHARS = 8000
MAX_PRE_CONTEXT_TURNS = 10  # 段首回溯客户轮上限


@dataclass
class MultiMessage:
    turn_no: int
    role: str              # "客" | "助"
    speaker: str           # 原始发送人/标记词
    canonical_name: str    # 助侧规范名；客户侧 "客户"
    text: str
    timestamp: str | None = None
    assistant_id: int | None = None
    raw_line: str = ""     # 原始行（可追溯）


@dataclass
class Segment:
    turns: list[Turn]          # 送 run_inspection（保留绝对 turn_no）
    context_turns: list[Turn]  # 段外前文 + 块尾后客户轮（仅参考不计分）
    text: str                  # to_numbered_text 产物（落库 raw_dialogue）
    context_text: str | None   # 上下文编号文本（注入 prompt 上下文区）
    evaluation_context: dict   # {reply_count, reply_turn_nos, context_turn_nos,
                               #  feedback_turn_nos, customer_turn_count}
    start_turn: int
    end_turn: int


@dataclass
class AssistantCluster:
    canonical_name: str        # mapping 的键（归并后规范名）
    display_name: str          # 匹配员工 → assistant.name；未匹配 → canonical_name
    aliases: list[str]         # 原始 speaker 词集合（同人异名）
    assistant_id: int | None
    reply_turn_nos: list[int]
    segment: Segment | None = None


@dataclass
class MultiParseResult:
    messages: list[MultiMessage] = field(default_factory=list)
    clusters: list[AssistantCluster] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fmt: str = "text"
    role_stats: dict = field(default_factory=dict)
    raw_text: str = ""


def parse_multi(raw_text: str, assistant_db=None) -> MultiParseResult:
    """解析完整聊天记录 → 结构化消息 + 助理识别/归并 + 员工匹配 + 服务分段。

    assistant_db：员工列表（鸭子类型，元素含 .id/.name/.employee_no 即可），
    缺省时不做员工匹配（assistant_id 全为 None，由前端人工指定）。
    """
    text = (raw_text or "").strip()
    if not text:
        raise ParseError("内容为空，请粘贴或上传会话记录")
    if text.lstrip().startswith("[") and _looks_like_json(text):
        messages = _parse_json_messages(text, assistant_db)
        fmt = "json"
    elif _looks_like_csv(text):
        messages = _parse_csv_messages(text, assistant_db)
        fmt = "csv"
    else:
        messages = _parse_text_messages(text, assistant_db)
        fmt = "text"
    if not messages:
        raise ParseError(
            "未能识别对话角色与轮次。请确认文本包含 [客]/[助]、客：/助： 等角色标记，"
            "或导出文本（客户 昵称 + 时间戳 + 内容），或改用 JSON/CSV 格式导入"
        )
    result = MultiParseResult(messages=messages, fmt=fmt, raw_text=text)
    result.role_stats = {
        "客": sum(1 for m in messages if m.role == "客"),
        "助": sum(1 for m in messages if m.role == "助"),
        "total": len(messages),
    }
    clusters = _cluster_assistants(messages, assistant_db)
    _build_segments(result, clusters)
    result.clusters = clusters
    _collect_warnings(result)
    return result


# ---------- 格式嗅探 ----------

def _looks_like_json(text: str) -> bool:
    try:
        return isinstance(json.loads(text), list)
    except json.JSONDecodeError:
        return False


def _looks_like_csv(text: str) -> bool:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    if "," not in first_line and "\t" not in first_line:
        return False
    lower = first_line.lower()
    return any(
        h in lower
        for h in ("角色", "发言人", "说话人", "发送人", "内容", "消息", "role", "speaker", "content", "text")
    )


# ---------- 文本逐行解析 ----------

def _parse_text_messages(text: str, assistant_db) -> list[MultiMessage]:
    """marker 文本 / 块头导出文本 / 微信双行格式逐行解析，时间戳原样保留。"""
    messages: list[MultiMessage] = []
    lines = text.splitlines()
    pending_name: tuple[str, str] | None = None  # (role, speaker_raw)：待下一内容行填充的 sender 轮
    preamble: list[str] = []

    def _flush_pending(content: str, line: str) -> None:
        nonlocal pending_name
        role, spk = pending_name
        messages.append(
            MultiMessage(
                turn_no=len(messages) + 1, role=role, speaker=spk,
                canonical_name=spk if role == "助" else "客户",
                text=content.strip(), timestamp=_pending_ts, raw_line=line,
            )
        )
        pending_name = None

    _pending_ts: str | None = None
    _skip_next = False  # 三行式（时间戳行/发送人行/内容行）：跳过已并入 pending 的发送人行
    last_was_header = False  # 上一行是块头声明（①）：下一时间戳行应挂载，不参与三行式判定
    header_awaiting_content = False  # 块头+时间戳轮已建、等待内容行：内容行优先续入（防被 marker/未知说话人劫持）
    for i, raw_line in enumerate(lines):
        if _skip_next:
            _skip_next = False
            continue
        line = raw_line.strip()
        if not line:
            continue
        next_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
        next_is_ts = bool(next_line) and bool(_TS_FULL_RE.match(next_line))
        # 行首时间戳剥离（其余分支都在 rest 上匹配）
        m_ts = _TIMESTAMP_RE.match(line)
        if m_ts:
            rest = line[m_ts.end():].strip()
            line_ts = m_ts.group(1)
        else:
            rest = line
            line_ts = None
        if pending_name:
            # 待填充 sender 轮：下一非时间戳行即内容
            if _TS_FULL_RE.match(line):
                _pending_ts = line
                continue
            _flush_pending(line, raw_line)
            continue
        # ① 块头声明行：客户|助理 + 昵称/姓名（冒号可选、行尾可粘连时间戳）。
        #    判定必须 下一行是时间戳 或 本行尾部带时间戳——否则"客户 你好"这类 marker 会被误吞
        #    （第一版 _MARKER_RE 分隔符可选，会把"客户 哈尔滨赢家1122"整个当 marker 轮）。
        header = _HEADER_LINE_RE.match(rest)
        name_part, tail_ts = _strip_tail_timestamp(header.group(2)) if header else (None, None)
        if header and normalize_role(header.group(1)) and (next_is_ts or tail_ts):
            role = normalize_role(header.group(1))
            name = (name_part or "").strip()
            messages.append(
                MultiMessage(
                    turn_no=len(messages) + 1, role=role,
                    speaker=name, canonical_name=name if role == "助" else "客户",
                    text="", timestamp=tail_ts or line_ts, raw_line=raw_line,
                )
            )
            last_was_header = True
            header_awaiting_content = True
            continue
        # ①b 块头空轮内容优先续入：块头+时间戳之后的下一内容行是本轮内容（含"王萌：帮我看看"、
        #    "老师：这个票怎么样"等冒号行），不得被 ②/⑤ 劫持成新轮；"时间戳+发送人"型行视为新消息放行
        if header_awaiting_content and not _TS_FULL_RE.match(line):
            if m_ts and _NAME_LINE_RE.match(rest):
                header_awaiting_content = False  # ④ 型新消息，交 ④ 处理
            else:
                if messages:
                    messages[-1].text = f"{messages[-1].text}\n{line}".strip()
                    messages[-1].raw_line = raw_line
                header_awaiting_content = False
                continue
        # ② 标准角色标记轮（[客] / 客： / 客服A： 等）
        #    要求显式分隔符（sep=True）——"客气了，有收获就好"这类以角色词开头的
        #    普通句子会被宽匹配吃字（"客"+"气了…"），拒绝后落入内容行续入上一条
        marker = match_marker(rest)
        if marker and marker.get("sep") and normalize_role(marker["role"]):
            role = normalize_role(marker["role"])
            suffix = marker.get("suffix") or ""
            spk = marker["role"].strip() + suffix
            messages.append(
                MultiMessage(
                    turn_no=len(messages) + 1, role=role, speaker=spk,
                    canonical_name=spk if role == "助" else "客户",
                    text=marker["text"].strip(), timestamp=line_ts, raw_line=raw_line,
                )
            )
            continue
        # ③ 整行时间戳 → 挂到当前轮；若上一行是块头声明（①），只挂载不参与三行式判定
        #    （"块头行/时间戳行/内容行"中内容行可能是 2-4 字短句，不得误判为发送人）；
        #    否则若下一行是发送人行（三行式导出：时间戳/发送人/内容），进入 pending 模式
        if _TS_FULL_RE.match(line):
            if last_was_header:
                last_was_header = False
                if messages:
                    messages[-1].timestamp = line
                continue
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if nxt and (i + 2 < len(lines)) and _looks_like_sender_line(nxt, assistant_db):
                pending_name = (_infer_role(nxt, assistant_db), _strip_role_prefix(nxt))
                _pending_ts = line
                _skip_next = True
                continue
            if messages:
                messages[-1].timestamp = line
            continue
        # ④ 微信双行 sender 行：行首时间戳 + 发送人（无角色词无冒号）
        if m_ts and _NAME_LINE_RE.match(rest):
            name = rest.strip()
            # 短回应词（"2026-08-24 10:25:00 好的"）：不是新消息的发送人，时间戳挂上一轮、
            # 短回应续入上一轮内容（无发送人两行式的固有歧义）
            if _is_short_acknowledgment(name):
                if messages:
                    messages[-1].timestamp = line_ts
                    messages[-1].text = f"{messages[-1].text}\n{name}".strip()
                continue
            role = _infer_role(name, assistant_db)
            pending_name = (role, name)
            _pending_ts = line_ts
            continue
        # ⑤ 未知说话人行："名字：内容"（时间戳剥离后同样适用）
        unk = _UNKNOWN_LINE_RE.match(rest)
        if unk:
            name, txt = unk.group("name"), unk.group("text")
            role = _infer_role(name, assistant_db)
            messages.append(
                MultiMessage(
                    turn_no=len(messages) + 1, role=role, speaker=name,
                    canonical_name=name if role == "助" else "客户",
                    text=txt.strip(), timestamp=line_ts, raw_line=raw_line,
                )
            )
            continue
        # ⑤b 方括号裸名说话人："[王萌] 内容"（非角色词 → 助理候选，匹配员工则绑定）
        brk = _BRACKET_NAME_RE.match(rest)
        if brk:
            name, txt = brk.group("name").strip(), brk.group("text").strip()
            role = _infer_bracket_role(name, assistant_db)
            messages.append(
                MultiMessage(
                    turn_no=len(messages) + 1, role=role, speaker=name,
                    canonical_name=name if role == "助" else "客户",
                    text=txt, timestamp=line_ts, raw_line=raw_line,
                )
            )
            continue
        # ⑥ 内容行 → 续入上一轮
        if messages:
            messages[-1].text = f"{messages[-1].text}\n{line}".strip()
            messages[-1].raw_line = raw_line
        else:
            preamble.append(line)
    if pending_name:
        _flush_pending("", "")
    return messages


def _strip_tail_timestamp(s: str) -> tuple[str, str | None]:
    """剥离块头行尾部粘连的时间戳："哈尔滨赢家11222026-05-29 15:30:14" → ("哈尔滨赢家1122", ts)。

    支持毫秒 / 无年份（必须带时间，防"10.25 补仓提醒"内容行）/ T 分隔。带年份日期允许纯日期
    （块头"客户 王先生 2026-08-24"）。"""
    m = re.search(
        r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?)?"
        r"|\d{1,2}[-/.]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?)$",
        s,
    )
    if m:
        return s[: m.start()].strip(), m.group(1)
    return s.strip(), None


# ---------- CSV / JSON ----------

def _parse_csv_messages(text: str, assistant_db) -> list[MultiMessage]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        raise ParseError("CSV 内容为空")
    header = [c.strip().lower() for c in rows[0]]
    role_idx = text_idx = time_idx = None
    for i, cell in enumerate(header):
        if cell in {"角色", "发言人", "说话人", "身份", "发送人", "发送者", "发件人", "昵称", "姓名", "名字", "role", "speaker", "who", "sender", "from", "send_from"}:
            if role_idx is None:
                role_idx = i
        elif cell in {"内容", "消息", "对话", "正文", "聊天内容", "消息内容", "会话内容", "发言内容", "回复内容", "文本", "content", "text", "message", "msg", "details", "msg_content"}:
            if text_idx is None:
                text_idx = i
        elif cell in _TIME_HEADERS:
            time_idx = i
    if role_idx is None or text_idx is None:
        if len(header) >= 2 and not any(
            h in {"角色", "发言人", "说话人", "身份", "发送人", "昵称", "姓名", "内容", "消息", "role", "speaker", "content", "text"}
            for h in header
        ):
            role_idx, text_idx = 0, 1
        else:
            raise ParseError("CSV 缺少可识别表头：需要「发送人/角色」「内容」两列（可含「时间」列）")
    messages: list[MultiMessage] = []
    for row in rows[1:]:
        if len(row) <= max(i for i in (role_idx, text_idx) if i is not None):
            continue
        speaker_raw = str(row[role_idx]).strip()
        content = str(row[text_idx]).strip()
        if not content:
            continue
        role = _resolve_speaker_role(speaker_raw, assistant_db)
        if role is None:
            continue
        ts = None
        if time_idx is not None and len(row) > time_idx and str(row[time_idx]).strip():
            ts = str(row[time_idx]).strip()
        messages.append(
            MultiMessage(
                turn_no=len(messages) + 1, role=role, speaker=speaker_raw,
                canonical_name=speaker_raw if role == "助" else "客户",
                text=content, timestamp=ts, raw_line=",".join(row),
            )
        )
    return messages


def _parse_json_messages(text: str, assistant_db) -> list[MultiMessage]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"JSON 解析失败：{exc}") from exc
    if not isinstance(data, list) or not data:
        raise ParseError("JSON 需为非空数组")
    messages: list[MultiMessage] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ParseError(f"JSON 第 {i + 1} 项不是对象")
        role_raw = item.get("role") or item.get("speaker") or item.get("角色") or item.get("发言人") or item.get("发送人")
        text_raw = item.get("content") or item.get("text") or item.get("message") or item.get("内容")
        ts = item.get("time") or item.get("时间") or item.get("timestamp")
        if role_raw is None or text_raw is None:
            raise ParseError(f"JSON 第 {i + 1} 项缺少 role/content 字段")
        role = _resolve_speaker_role(str(role_raw), assistant_db)
        if role is None:
            continue
        content = str(text_raw).strip()
        if not content:
            continue
        messages.append(
            MultiMessage(
                turn_no=len(messages) + 1, role=role, speaker=str(role_raw).strip(),
                canonical_name=str(role_raw).strip() if role == "助" else "客户",
                text=content, timestamp=str(ts) if ts is not None else None,
                raw_line=json.dumps(item, ensure_ascii=False),
            )
        )
    return messages


# ---------- 角色推断 / 规范化 ----------

def _resolve_speaker_role(speaker_raw: str, assistant_db) -> str | None:
    """发送人 → 客|助|None。优先级：规范角色词 → 前缀模式 → 员工匹配/助词素推断 → 方向语义。"""
    role = normalize_role(speaker_raw)
    if role:
        return role
    if _ASSISTANT_PREFIX_RE.match(speaker_raw):
        return "助"
    if _CUSTOMER_PREFIX_RE.match(speaker_raw):
        return "客"
    return _infer_role(speaker_raw, assistant_db)


def _infer_role(speaker: str, assistant_db) -> str:
    """无角色词的发送人：员工表命中/含助词素/中文人名（含"班级名（姓名）"）→ 助；否则客。

    中文人名排除称谓词（王先生/张女士/李老师）——客户也可能是中文名。
    称谓检查必须作用于原始字符串：_canonicalize 会把"王老师"剥成"王"（2 字中文名），
    先剥后查会误判。宁可标为"未匹配助理"留给前端人工归属，也不要把助理吞成客户轮。
    """
    if _match_employee(speaker, assistant_db):
        return "助"
    low = speaker.lower()
    if any(w in low for w in _AUTO_ASSISTANT_WORDS):
        return "助"
    if _HONORIFIC_RE.search(speaker) or _is_short_acknowledgment(speaker):
        return "客"
    cn = _canonicalize(speaker)
    if _is_chinese_name(cn):
        return "助"
    return "客"


def _looks_like_sender_line(nxt: str, assistant_db) -> bool:
    """三行式导出（时间戳行 / 发送人行 / 内容行）中发送人行的判定。

    特征：员工名/含助词素/客户前缀（"客户哈尔滨赢家1122"）/数字或字母昵称/
    2-4 字中文名（含"班级名（姓名）"剥括号后）。不含称谓词（王先生/李老师）。
    刻意排除长中文句子（"我会持续关注"）——两行式"时间戳行+内容行"的内容
    不得被误判为发送人。
    """
    if not _SENDER_LINE_RE.match(nxt):
        return False
    if _match_employee(nxt, assistant_db):
        return True
    # 短回应词（"好的/嗯/666"）是客户回复内容，不得建发送人轮（员工命中在前，工号不受影响）
    if _is_short_acknowledgment(nxt):
        return False
    low = nxt.lower()
    if any(w in low for w in _AUTO_ASSISTANT_WORDS):
        return True
    if _CUSTOMER_PREFIX_RE.match(nxt):
        return True
    if re.search(r"[0-9a-zA-Z]", nxt):
        return True
    # 称谓检查作用原始字符串（同 _infer_role 理由：先剥会把"好的老师"剥成"好的"）
    if _HONORIFIC_RE.search(nxt):
        return False
    cn = _canonicalize(nxt)
    return _is_chinese_name(cn) and len(cn) <= 4


def _infer_bracket_role(name: str, assistant_db) -> str:
    """方括号裸名（[王萌] 内容）：员工/助词素/中文人名 → 助（助理候选，未匹配留待人工归属）；
    数字昵称等（哈尔滨赢家1122）→ 客。"""
    if _match_employee(name, assistant_db):
        return "助"
    low = name.lower()
    if any(w in low for w in _AUTO_ASSISTANT_WORDS):
        return "助"
    if _is_chinese_name(name):
        return "助"
    return "客"


def _is_chinese_name(s: str) -> bool:
    return bool(s) and len(s) <= 8 and all("一" <= ch <= "鿿" or ch == "·" for ch in s)


_ROLE_PREFIX_WORDS = (
    "客户", "会员", "用户", "顾客", "学员", "投资者", "投资用户",
    "客服", "投顾", "助理", "顾问", "理财师", "座席", "运营", "班主任", "管理员",
)


def _strip_role_prefix(s: str) -> str:
    """剥发送人行角色前缀（"客户哈尔滨赢家1122" → "哈尔滨赢家1122"），与块头式 speaker 口径一致。"""
    n = s.strip()
    for pfx in _ROLE_PREFIX_WORDS:
        if n.startswith(pfx) and len(n) > len(pfx):
            return n[len(pfx):].strip()
    return n


def _canonicalize(speaker: str) -> str:
    """助侧说话人规范化：括号班级名 → 取姓名；称谓前后缀剥离；编号/纯角色词保留。"""
    n = speaker.strip()
    if not n:
        return n
    # 括号班级名："韩珂龙头班（王萌）" → "王萌"；"韩珂龙头班(王萌)" 同理
    m = re.search(r"[（(]([^（）()]+)[）)]$", n)
    if m and _is_chinese_name(m.group(1)):
        n = m.group(1)
    # 称谓后缀：马萌老师 / 老师马萌 → 马萌
    for prefix in ("老师", "客服", "投顾", "助理", "顾问", "理财师", "座席", "运营", "班主任", "管理员"):
        if n.startswith(prefix) and len(n) > len(prefix):
            rest = n[len(prefix):].strip()
            if _is_chinese_name(rest):
                return rest
        if n.endswith(prefix) and len(n) > len(prefix):
            rest = n[:-len(prefix)].strip()
            if _is_chinese_name(rest):
                return rest
    return n


def _match_employee(speaker: str, assistant_db) -> tuple[int, object] | None:
    """员工表匹配（确定性，宁缺毋滥）：返回 (优先级, 员工)。

    优先级：employee_no 精确 → name 精确 → 清理后相等 → 互含（长度≥2）→ employee_no 包含。
    命中多条或零条 → None。
    """
    if not assistant_db:
        return None
    cand = speaker.strip().lower()
    cand_clean = _canonicalize(speaker).lower()
    hits: list[tuple] = []
    for ast in assistant_db:
        name = (ast.name or "").strip().lower()
        no = (ast.employee_no or "").strip().lower()
        score = None
        if no and cand == no:
            score = 5
        elif name and cand == name:
            score = 4
        elif name and cand_clean and cand_clean == name:
            score = 3
        # 互含/工号包含仅对 ≥2 字候选生效：单字"徐"是姓，命中可能是巧合，宁缺毋滥
        elif name and len(cand_clean) >= 2 and len(name) >= 2 and (name in cand_clean or cand_clean in name):
            score = 2
        elif no and len(cand) >= 2 and no in cand:
            score = 1
        if score:
            hits.append((score, ast))
    if len(hits) == 1:
        return hits[0]
    return None


# ---------- 归并 / 聚类 ----------

def _cluster_assistants(messages: list[MultiMessage], assistant_db) -> list[AssistantCluster]:
    """助侧聚类：员工命中合并 → 纯角色词编号 → 同规范名合并 → 同尾部标识符合并。"""
    # 第一步：员工匹配（宁缺毋滥：命中多条 → None）
    for m in messages:
        if m.role != "助":
            continue
        hit = _match_employee(m.speaker, assistant_db)
        if hit:
            ast = hit[1]
            m.assistant_id = ast.id
            m.canonical_name = ast.name
        else:
            m.assistant_id = None
            m.canonical_name = _canonicalize(m.speaker)
    # 第二步：分组 key = 员工 id（命中）或规范名
    groups: dict[str, dict] = {}
    order: list[str] = []
    for m in messages:
        if m.role != "助":
            continue
        if m.assistant_id:
            key = f"e:{m.assistant_id}"
        else:
            key = f"n:{m.canonical_name}"
        if key not in groups:
            groups[key] = {"name": m.canonical_name, "aid": m.assistant_id, "speakers": []}
            order.append(key)
        if m.speaker not in groups[key]["speakers"]:
            groups[key]["speakers"].append(m.speaker)
    # 第三步：纯角色词（客服/助理/投顾…无编号）按出现顺序编号 客服A/客服B…
    #   （避免与 evaluatee 命名不一致；编号后不再是纯角色词，parse_turns 不会改写）
    base_counter: dict[str, int] = {}
    auto_numbered: set[str] = set()  # 自动编号的组不参与第四步尾部合并（编号已区分身份）
    for key in order:
        g = groups[key]
        if g["aid"] or _IDENT_TAIL_RE.search(g["name"]):
            continue
        if normalize_role(g["name"]) != "助":
            continue
        base = g["name"]
        base_counter[base] = base_counter.get(base, 0) + 1
        label = f"{base}{chr(64 + base_counter[base])}"
        g["name"] = label
        auto_numbered.add(key)
        for m in messages:
            if m.role == "助" and not m.assistant_id and m.canonical_name == base:
                m.canonical_name = label
    # 第四步：无员工命中的组按尾部标识符合并（客服A + 投顾A → 同一人）
    merged: dict[str, dict] = {}
    for key in order:
        g = groups[key]
        if g["aid"] or key in auto_numbered:
            merged[key] = g
            continue
        tail = _IDENT_TAIL_RE.search(g["name"])
        matched_key = None
        if tail:
            for other_key in list(merged.keys()):
                og = merged[other_key]
                if og["aid"]:
                    continue
                ot = _IDENT_TAIL_RE.search(og["name"])
                if ot and ot.group() == tail.group():
                    matched_key = other_key
                    break
        if matched_key:
            merged[matched_key]["speakers"].extend(s for s in g["speakers"] if s not in merged[matched_key]["speakers"])
            # 同人异名合并：被并组的消息 canonical_name 统一为保留组名字，回复计数/分段才能归并
            for m in messages:
                if m.role == "助" and not m.assistant_id and m.canonical_name == g["name"]:
                    m.canonical_name = merged[matched_key]["name"]
        else:
            merged[key] = g
    clusters = [
        AssistantCluster(
            canonical_name=g["name"],
            display_name=g["name"],  # 有员工 → 用员工名（下面回填）
            aliases=list(dict.fromkeys(g["speakers"])),
            assistant_id=g["aid"],
            reply_turn_nos=[m.turn_no for m in messages if m.role == "助" and (
                (g["aid"] and m.assistant_id == g["aid"])
                or (not g["aid"] and m.canonical_name == g["name"])
            )],
        )
        for g in merged.values()
    ]
    for c in clusters:
        if c.assistant_id:
            emp = next((e for e in (assistant_db or []) if e.id == c.assistant_id), None)
            if emp:
                c.display_name = emp.name
    return clusters


# ---------- 分段 ----------

def _build_segments(result: MultiParseResult, clusters: list[AssistantCluster]) -> None:
    """每簇服务段落：段首回溯客户轮入段；段尾=块尾；块尾后客户轮入上下文（仅参考不计分）。

    客户轮归属规则：客户轮只归属"其后第一个助轮"（可隔客户轮）所在簇，只进该簇段 body——
    已被其他助理回答过的客户轮不得算进本簇（否则 LLM 会误判"该助理未回复此问题"）；
    对话末尾无任何后继助轮的客户轮才算本簇未答反馈。
    """
    messages = result.messages
    n = len(messages)
    # 预计算：助轮 turn_no → 所在簇（每个助轮恰属一簇）
    cluster_by_turn: dict[int, AssistantCluster] = {}
    for c in clusters:
        for no in c.reply_turn_nos:
            cluster_by_turn[no] = c
    # 预计算：客户轮 turn_no → 其后第一个助轮的簇（None = 对话末尾无后继助轮，真正无人回答）
    owner_of_turn: dict[int, AssistantCluster | None] = {}
    next_cluster: AssistantCluster | None = None
    for i in range(n - 1, -1, -1):
        m = messages[i]
        if m.role == "助":
            next_cluster = cluster_by_turn.get(m.turn_no)
        else:
            owner_of_turn[m.turn_no] = next_cluster
    # 各簇在 messages 中的索引范围（按簇的 reply_turn_nos）
    for c in clusters:
        idxs = [i for i, m in enumerate(messages) if m.turn_no in set(c.reply_turn_nos)]
        if not idxs:
            continue
        first, last = idxs[0], idxs[-1]
        # 段首回溯：块首前紧邻连续客户轮 → 连续段起点；否则跨助轮找最近客户轮
        start = first
        for i in range(first - 1, -1, -1):
            if messages[i].role != "客":
                break
            start = i
        if start == first:
            for i in range(first - 1, -1, -1):
                if messages[i].role == "客":
                    start = i
                    break
        if first - start > MAX_PRE_CONTEXT_TURNS:
            start = first - MAX_PRE_CONTEXT_TURNS
            result.warnings.append(f"「{c.display_name}」段首客户上下文超过 {MAX_PRE_CONTEXT_TURNS} 轮，已截断")
        # 块尾后至下一簇首条回复前的客户轮 → 上下文（用户口径：后文仅作参考，不算未回答）
        next_first = n
        for other in clusters:
            if other is c:
                continue
            other_first = next((i for i, m in enumerate(messages) if m.turn_no in set(other.reply_turn_nos)), None)
            if other_first is not None and first < other_first < next_first:
                next_first = other_first
        body = []
        context = []
        context_nos = []
        feedback_nos = []
        moved = []  # 段内归属其他簇的客户轮（他人已回答）→ 上下文，仅参考不计分
        # ① 段内：start..last 的本簇助轮 + 归属本簇的客户轮（其后第一个助轮是本簇）；
        #    归属其他簇的客户轮移入上下文——已被他人回答，不得算本簇"未回复"
        for i in range(start, last + 1):
            m = messages[i]
            is_own = m.role == "助" and (
                (c.assistant_id and m.assistant_id == c.assistant_id)
                or (not c.assistant_id and m.canonical_name == c.canonical_name)
            )
            if is_own:
                body.append(Turn(role="助", speaker=c.display_name, text=m.text, turn_no=m.turn_no))
            elif m.role == "客":
                if owner_of_turn.get(m.turn_no) is c:
                    body.append(Turn(role="客", speaker="客户", text=m.text, turn_no=m.turn_no))
                else:
                    moved.append(Turn(role="客", speaker="客户", text=m.text, turn_no=m.turn_no))
                    context_nos.append(m.turn_no)
        # ② 段外更早前文（0..start-1，保持原始顺序）→ 上下文
        for i in range(0, start):
            m = messages[i]
            if m.role == "客" or (m.role == "助" and not (
                (c.assistant_id and m.assistant_id == c.assistant_id)
                or (not c.assistant_id and m.canonical_name == c.canonical_name)
            )):
                context.append(Turn(role=m.role, speaker=m.canonical_name or m.speaker, text=m.text, turn_no=m.turn_no))
                context_nos.append(m.turn_no)
        # ②b 段内被移出的客户轮（他人已回答）→ 上下文，接在前文之后（保持时间顺序）
        context.extend(moved)
        # ③ 块尾后至下一簇首条回复前的客户轮 → 上下文（用户口径：后文仅作参考，不算未回答）；
        #    仅"无任何后继助轮"的客户轮（真正无人回答）才计为未答反馈
        for i in range(last + 1, next_first):
            m = messages[i]
            if m.role == "客":
                context.append(Turn(role="客", speaker="客户", text=m.text, turn_no=m.turn_no))
                context_nos.append(m.turn_no)
                if owner_of_turn.get(m.turn_no) is None:
                    feedback_nos.append(m.turn_no)
        # 上下文截断上限
        if len(context) > MAX_CONTEXT_TURNS:
            context = context[:MAX_CONTEXT_TURNS]
            context_nos = context_nos[:MAX_CONTEXT_TURNS]
            result.warnings.append(f"「{c.display_name}」上下文超过 {MAX_CONTEXT_TURNS} 轮，已截断（仅作参考）")
        text = to_numbered_text(body)
        context_text = None
        if context:
            ctx_text = (
                summarize_long_dialogue(context, keep_head=5, keep_tail=5)
                if len(context) > 40 else to_numbered_text(context)
            )
            if len(ctx_text) > MAX_CONTEXT_CHARS:
                ctx_text = ctx_text[:MAX_CONTEXT_CHARS] + "\n...（上下文过长已截断）"
            context_text = ctx_text
        c.segment = Segment(
            turns=body, context_turns=context,
            text=text, context_text=context_text,
            evaluation_context={
                "reply_count": len(c.reply_turn_nos),
                "reply_turn_nos": c.reply_turn_nos,
                "context_turn_nos": context_nos,
                "feedback_turn_nos": feedback_nos,
                "customer_turn_count": sum(1 for t in body if t.role == "客"),
            },
            start_turn=body[0].turn_no if body else 0,
            end_turn=body[-1].turn_no if body else 0,
        )


# ---------- 警告 ----------

def _collect_warnings(result: MultiParseResult) -> None:
    """生成 warning：未匹配助理 / 角色推断存疑 / 单助理 / 轮数少。"""
    for c in result.clusters:
        if not c.assistant_id:
            result.warnings.append(
                f"助理「{c.canonical_name}」未匹配到员工（{'、'.join(c.aliases)}），请选择归属员工或新建"
            )
    if len(result.clusters) > 1:
        result.warnings.append(
            f"检测到 {len(result.clusters)} 位助理轮替服务，可一键批量生成每位助理的质检报告"
        )
    if result.role_stats["客"] == 0:
        result.warnings.append("未识别到客户发言，请检查格式（D2 画像匹配等维度将难以评分）")
    if result.role_stats["助"] == 0:
        result.warnings.append("未识别到助理发言，请检查格式")
    if result.role_stats["total"] < 4:
        result.warnings.append(f"对话轮数较少（{result.role_stats['total']} 轮），可能影响评分准确性")
