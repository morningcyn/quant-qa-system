# 批量评分上下文 Chunk 管理：超长 Segment 滑动窗口切分
# 切分在 Segment 层（turns 子集）：chunk 直接是 list[Turn]（绝对 turn_no 保留，highlight 天然对齐）
# 切点尽量落在「上一条为助轮」的边界（客户问句与回复同窗）；重叠轮仅作上下文参考不计分
from dataclasses import dataclass, field

from backend.services.parser import Turn, to_numbered_text

DEFAULT_CHUNK_PARAMS = {
    "char_threshold": 30000,    # 30K 字 ≈ 30K tokens，64K 上下文下留足系统提示 + 上下文 + 8192 输出
    # 上下文感知（2026-08-31 按用户要求加大）：重叠轮数 = 窗口轮数 × ratio，重叠轮仅参考不计分，
    # 保证块与块之间保留更早的前文（客户诉求演变/助理此前解释），避免脱离语境盲目打分
    "overlap_ratio": 0.30,      # 重叠轮数 = 窗口轮数 × ratio（按轮不写死）
    "max_overlap_chars": 16000, # 重叠上下文字符上限（从窗口尾部向前裁）
    "max_turns_per_chunk": 60,  # 二级安全窗（避免走进 summarize 压缩路径）
    "force_max_chars": 120000,  # 单条消息硬上限：宁可拆开问答也不截断文本（单轮超长只能截断）
}


@dataclass
class Chunk:
    turns: list[Turn]           # 本窗轮次（绝对 turn_no）
    numbered_text: str          # 送模型的编号文本
    context_text: str | None    # 段外上下文 + 重叠轮（标注「仅参考不计分」）
    start_turn: int             # 首轮 turn_no
    end_turn: int               # 末轮 turn_no
    overlap_turn_nos: list = field(default_factory=list)  # 重叠轮（全局下标）


def chunk_segment(segment, params: dict | None = None) -> list[Chunk]:
    """Segment → Chunk 列表。

    短段（≤阈值且 ≤60 轮）直通单 Chunk；超长贪心窗口 + 重叠滑动；任一窗口 >60 轮再拆子窗。
    params 可从 settings 表读取（batch_chunk_params），缺省 DEFAULT_CHUNK_PARAMS。
    """
    merged = {**DEFAULT_CHUNK_PARAMS, **(params or {})}
    turns = list(segment.turns)
    if not turns:
        return []
    total_chars = sum(len(t.text) for t in turns)
    if total_chars <= merged["char_threshold"] and len(turns) <= merged["max_turns_per_chunk"]:
        return [_make_chunk(segment, turns, [], merged)]
    chunks: list[Chunk] = []
    prev_overlap: list[int] = []
    for start, end, overlap_idxs in _build_windows(turns, merged):
        window = turns[start:end]
        if len(window) > merged["max_turns_per_chunk"]:
            # 二级安全窗：>60 轮子窗（重叠 5 轮，切点后为助轮起点）
            subs = _split_window(window, merged["max_turns_per_chunk"])
            for j, (ss, se) in enumerate(subs):
                # 子窗开头 5 轮为上一子窗尾重叠（仅参考不计分）；首个子窗沿用窗口级头部重叠
                head = list(range(min(5, se - ss))) if j > 0 else [i - start for i in prev_overlap]
                chunks.append(_make_chunk(segment, window[ss:se], head, merged))
        else:
            # prev_overlap 为上一窗口尾部重叠（turns 全局下标）→ 本窗头部 → 转窗口内下标
            chunks.append(_make_chunk(segment, window, [i - start for i in prev_overlap], merged))
        prev_overlap = overlap_idxs
    return chunks


def _build_windows(turns: list[Turn], p: dict) -> list[tuple[int, int, list]]:
    """贪心窗口：[(start, end, overlap_idxs)]。start/end 为 turns 全局下标。"""
    n = len(turns)
    windows: list[tuple[int, int, list]] = []
    s = 0
    while s < n:
        total = 0
        end = s
        for i in range(s, n):
            total += len(turns[i].text)
            if total > p["char_threshold"]:
                break
            end = i + 1
        if end >= n:
            windows.append((s, n, []))
            break
        # 合法切点：turns[cut-1] 为助轮（上一条是助回复，问句与回复同窗）；最差回退 s+1 强制切
        cut = end
        while cut > s + 1 and turns[cut - 1].role != "助":
            cut -= 1
        if cut <= s:
            cut = s + 1  # 单条超长轮：窗口至少含 1 轮，避免空窗口
        # 重叠：窗口轮数 × ratio 轮，从尾部向前裁至 max_overlap_chars
        overlap_turns = max(1, round((cut - s) * p["overlap_ratio"]))
        ov: list[int] = []
        chars = 0
        for i in range(cut - 1, s - 1, -1):
            chars += len(turns[i].text)
            if chars > p["max_overlap_chars"] and len(ov) >= 1:
                break
            ov.append(i)
            if len(ov) >= overlap_turns:
                break
        overlap_idxs = sorted(ov)
        windows.append((s, cut, overlap_idxs))
        next_s = cut - len(ov)
        if next_s <= s:
            next_s = s + 1
        s = next_s
    return windows


def _split_window(turns: list[Turn], max_turns: int) -> list[tuple[int, int]]:
    """>60 轮子窗：每窗 ≤ max_turns，切点后为助轮起点，重叠 5 轮。"""
    subs: list[tuple[int, int]] = []
    s = 0
    n = len(turns)
    while n - s > max_turns:
        end = min(s + max_turns, n)
        cut = end
        while cut > s + 1 and turns[cut].role != "助":
            cut -= 1
        subs.append((s, cut))
        s = max(cut - 5, s + 1)
    subs.append((s, n))
    return subs


def _make_chunk(segment, turns: list[Turn], overlap_idxs: list[int], p: dict) -> Chunk:
    """overlap_idxs 为 turns（传入切片）内的下标 → 生成上下文文本并记录绝对 turn_no。"""
    numbered = to_numbered_text(turns, max_chars=p["force_max_chars"])
    parts: list[str] = []
    if segment.context_text:
        parts.append(segment.context_text)
    if overlap_idxs:
        lines = [
            f"[{turns[i].turn_no}][{turns[i].speaker}] {turns[i].text}"
            for i in overlap_idxs
            if i < len(turns)
        ]
        if lines:
            parts.append("（重叠上下文轮，仅参考不计分，请勿重复计分）\n" + "\n".join(lines))
    return Chunk(
        turns=turns,
        numbered_text=numbered,
        context_text="\n\n".join(parts) or None,
        start_turn=turns[0].turn_no,
        end_turn=turns[-1].turn_no,
        overlap_turn_nos=[turns[i].turn_no for i in overlap_idxs if i < len(turns)],
    )
