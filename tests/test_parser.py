import pytest

from backend.services.parser import ParseError, parse_raw, to_numbered_text


class TestMarkerText:
    def test_basic(self):
        result = parse_raw("[客] 你好\n[助] 您好，请问有什么可以帮您？")
        assert len(result.turns) == 2
        assert result.turns[0].role == "客"
        assert result.turns[1].role == "助"
        assert result.role_stats["客"] == 1

    def test_match_marker_sep_field(self):
        """match_marker 的 sep：显式分隔符（闭括号/冒号/空格）为 True；
        角色词与内容紧贴（"客气了…"吃字风险）为 False，供多人解析拒绝。"""
        from backend.services.parser import match_marker

        assert match_marker("[客] 你好")["sep"] is True
        assert match_marker("客：你好")["sep"] is True
        assert match_marker("客 你好")["sep"] is True
        assert match_marker("客服A：您好")["sep"] is True
        assert match_marker("客气了，有收获就好")["sep"] is False
        assert match_marker("客户哈尔滨赢家1122")["sep"] is False  # 无分隔紧贴 → 吃字风险

    def test_colon_and_bracket_variants(self):
        result = parse_raw("客：你好\n【助】您好\n客户: 在吗\n(助) 在的")
        roles = [t.role for t in result.turns]
        assert roles == ["客", "助", "客", "助"]

    def test_continuation_lines_merge(self):
        result = parse_raw("[客] 第一行\n第二行续写\n[助] 回复")
        assert result.turns[0].text == "第一行\n第二行续写"
        assert len(result.turns) == 2

    def test_too_few_turns_warning(self):
        result = parse_raw("[客] 你好")
        assert any("轮数较少" in w for w in result.warnings)

    def test_missing_role_warning(self):
        result = parse_raw("[助] 您好\n[助] 在的\n[助] 好的\n[助] 谢谢")
        assert any("客户发言" in w for w in result.warnings)

    def test_garbage_raises(self):
        with pytest.raises(ParseError):
            parse_raw("这是一段没有角色标记的普通文本。\n第二行也是。")

    def test_numbered_text(self):
        result = parse_raw("[客] 你好\n[助] 您好")
        text = to_numbered_text(result.turns)
        assert "[1][客户] 你好" in text
        assert "[2][助理A] 您好" in text

    def test_speaker_assignment(self):
        # 客户侧统一为"客户"；助侧纯角色词按出现顺序编号为 助理A/助理B
        result = parse_raw("[客] 你好\n[助] 您好\n[投顾] 有什么可以帮您？")
        assert result.speakers == ["助理A", "助理B"]
        assert [t.speaker for t in result.turns] == ["客户", "助理A", "助理B"]

    def test_speaker_numbering_keeps_named(self):
        # 已带编号/人名的角色词保留原样；同一原始词复用同编号
        result = parse_raw("[客服A] 您好\n[客服B] 我在\n[客服A] 好的")
        assert result.speakers == ["客服A", "客服B"]
        assert [t.speaker for t in result.turns] == ["客服A", "客服B", "客服A"]

    def test_kefu_is_assistant_side(self):
        # "客服" 属于助理侧（客服人员），不再是客户角色词
        result = parse_raw("[客服] 您好\n[客户] 在吗\n[客服] 在的")
        assert [t.role for t in result.turns] == ["助", "客", "助"]
        assert result.speakers == ["助理A"]
        assert result.turns[0].speaker == "助理A"

    def test_multi_assistant_warning(self):
        result = parse_raw("[投顾] 您好\n[顾问] 您好")
        assert any("多位助理" in w for w in result.warnings)


class TestCsv:
    def test_header_aliases(self):
        result = parse_raw("role,content\n客,你好\n助,您好")
        assert [t.role for t in result.turns] == ["客", "助"]

    def test_chinese_headers(self):
        result = parse_raw("角色,内容\n客户,你好\n投顾,您好")
        assert [t.role for t in result.turns] == ["客", "助"]

    def test_positional_fallback(self):
        result = parse_raw("客,你好\n助,您好")
        assert [t.role for t in result.turns] == ["客", "助"]


class TestJson:
    def test_role_content(self):
        result = parse_raw('[{"role": "客", "content": "你好"}, {"role": "助", "content": "您好"}]')
        assert [t.role for t in result.turns] == ["客", "助"]

    def test_speaker_text(self):
        result = parse_raw('[{"speaker": "customer", "text": "hi"}, {"speaker": "assistant", "text": "hello"}]')
        assert [t.role for t in result.turns] == ["客", "助"]


class TestLongDialogue:
    def test_summary(self):
        from backend.services.parser import summarize_long_dialogue

        result = parse_raw("\n".join(f"[{('客' if i % 2 == 0 else '助')}] 第{i}轮内容" for i in range(80)))
        text = summarize_long_dialogue(result.turns)
        assert "（摘要）" in text
        assert "[1][客户]" in text
        assert "[80][助理A]" in text
