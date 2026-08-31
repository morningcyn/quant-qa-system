# 上下文 Chunk：短段直通 / 切点助轮尾 / 重叠轮 / 绝对 turn_no / 60 轮安全窗 / 单条超长强制切
from backend.services.batch.chunker import chunk_segment
from backend.services.multiparser import Segment
from backend.services.parser import Turn


def make_segment(n, char_len=100, start_alt=True):
    """交替 客/助 轮（turn_no 1..n），每轮 char_len 字。"""
    turns = []
    for i in range(n):
        role = "客" if (i % 2 == 0) else "助"
        turns.append(
            Turn(
                role=role,
                speaker="客户" if role == "客" else "助理A",
                text="字" * char_len,
                turn_no=i + 1,
            )
        )
    return Segment(
        turns=turns,
        context_turns=[],
        text="",
        context_text="【上下文】段外前文",
        evaluation_context={},
        start_turn=1,
        end_turn=n,
    )


class TestChunkSegment:
    def test_short_segment_single_chunk(self):
        """短段直通：≤阈值且 ≤60 轮 → 单 Chunk，绝对轮次保留。"""
        seg = make_segment(5, 100)
        chunks = chunk_segment(seg)
        assert len(chunks) == 1
        assert chunks[0].start_turn == 1 and chunks[0].end_turn == 5
        assert "[1][客户]" in chunks[0].numbered_text
        assert chunks[0].context_text  # 段外上下文保留
        assert chunks[0].overlap_turn_nos == []

    def test_cut_lands_after_assistant_turn(self):
        """超长切分：切点（首个非重叠轮）前一条必须为助轮（客户问句与回复同窗）。"""
        seg = make_segment(40, 2000)  # 40×2000=80000 字
        chunks = chunk_segment(seg)
        assert len(chunks) >= 2
        for i in range(1, len(chunks)):
            overlap = set(chunks[i].overlap_turn_nos)
            # 首个非重叠轮 = 新内容起点；其前一条在上一窗口且为助轮
            first_new = next(t for t in chunks[i].turns if t.turn_no not in overlap)
            last_of_prev = chunks[i - 1].turns[-1]
            assert last_of_prev.turn_no == first_new.turn_no - 1
            assert last_of_prev.role == "助"

    def test_overlap_ratio_rounds(self):
        """重叠轮数 = 窗口轮数 × ratio（四舍五入），含轮次记录。"""
        seg = make_segment(40, 2000)
        chunks = chunk_segment(seg)
        assert len(chunks) >= 2
        c0 = chunks[0]
        # 第 16 轮（32000 字）超 30000 阈值；回退到 turns[13]（助轮）结尾 → 窗口 [1..14]
        assert c0.start_turn == 1 and c0.end_turn == 14
        assert c0.turns[-1].role == "助"
        # overlap = round(14×0.30) = 4 轮；首窗头部无重叠，重叠标注属于下一窗头部
        assert c0.overlap_turn_nos == []
        # 第二窗从 11 轮开始（上一窗尾重叠轮起）——重叠翻倍保留更早前文（上下文感知）
        assert chunks[1].start_turn == 11
        assert set(chunks[1].overlap_turn_nos) == {11, 12, 13, 14}
        # 重叠轮进入 context_text 且标注仅参考不计分
        assert "仅参考不计分" in (chunks[1].context_text or "")
        assert "[11]" in chunks[1].context_text and "[14]" in chunks[1].context_text

    def test_turn_no_preserved_across_chunks(self):
        """多 chunk 后 turn_no 仍为原会话绝对轮次（highlight 对齐），窗口内单调。"""
        seg = make_segment(40, 2000)
        chunks = chunk_segment(seg)
        for c in chunks:
            nos = [t.turn_no for t in c.turns]
            assert nos == sorted(nos)
            assert all(1 <= no <= 40 for no in nos)
        assert "[40]" in chunks[-1].numbered_text

    def test_sixty_turn_safety_window(self):
        """61 轮 100 字（不超字符阈值）→ 60 轮安全窗拆 2 子窗（重叠 5 轮，切点后为助轮起点）。"""
        seg = make_segment(61, 100)
        chunks = chunk_segment(seg)
        assert len(chunks) == 2
        # 第 60 轮（下标 59，奇数 → 助）→ 切点 59；子窗2 从 54 轮起（重叠 5 轮）
        assert len(chunks[0].turns) == 59
        assert len(chunks[1].turns) == 7  # 轮次 55..61
        assert chunks[1].start_turn == 55
        assert chunks[1].overlap_turn_nos == [55, 56, 57, 58, 59]
        # 重叠轮 55..59 为「仅参考」，首个非重叠轮（turn 60）为助轮起点
        first_new = next(t for t in chunks[1].turns if t.turn_no not in chunks[1].overlap_turn_nos)
        assert first_new.turn_no == 60 and first_new.role == "助"
        assert "仅参考不计分" in (chunks[1].context_text or "")

    def test_single_oversized_turn_forced_cut(self):
        """单条超长轮（50000 字 > 阈值）→ 强制切为独立窗口（消息边界优先于截断）。"""
        seg = make_segment(2, 50000)
        chunks = chunk_segment(seg)
        assert len(chunks) == 2
        assert len(chunks[0].turns) == 1 and len(chunks[1].turns) == 1
        assert chunks[0].end_turn == 1 and chunks[1].start_turn == 2

    def test_empty_segment(self):
        seg = make_segment(0)
        assert chunk_segment(seg) == []

    def test_custom_params_respected(self):
        """自定义参数（如 5000 阈值）生效（settings 表 batch_chunk_params 可覆盖）。"""
        seg = make_segment(20, 500)  # 10000 字
        chunks = chunk_segment(seg, {"char_threshold": 5000})
        assert len(chunks) >= 2
