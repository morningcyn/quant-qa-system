import pytest

from backend.services.parser import ParseError, parse_raw, to_numbered_text


class TestMarkerText:
    def test_basic(self):
        result = parse_raw("[客] 你好\n[助] 您好，请问有什么可以帮您？")
        assert len(result.turns) == 2
        assert result.turns[0].role == "客"
        assert result.turns[1].role == "助"
        assert result.role_stats["客"] == 1

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
        assert "[1][客] 你好" in text
        assert "[2][助] 您好" in text


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
        assert "[1][客]" in text
        assert "[80][助]" in text
