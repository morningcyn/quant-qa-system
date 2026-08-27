# 多人解析模块：消息结构化 / 时间戳 / 角色推断 / 归并 / 员工匹配 / 分段 / 评分素材
import pytest

from backend.services.multiparser import MultiParseResult, parse_multi
from backend.services.parser import ParseError, parse_turns


class Emp:
    """测试用员工替身（鸭子类型：id/name/employee_no）。"""

    def __init__(self, id, name, employee_no):
        self.id, self.name, self.employee_no = id, name, employee_no


EMPLOYEES = [Emp(1, "王萌", "E001"), Emp(2, "徐艺桐", "E002")]


class TestMessageStructuring:
    def test_marker_multi_assistant_fields(self):
        raw = "[客] 你好\n[助理A] 您好\n[客] 基金亏了\n[助理B] 我来帮您查\n[客] 谢谢"
        r = parse_multi(raw)
        assert r.role_stats == {"客": 3, "助": 2, "total": 5}
        # 可追溯：turn_no 与原始行一一对应，text 逐字
        assert [m.turn_no for m in r.messages] == [1, 2, 3, 4, 5]
        assert r.messages[0].text == "你好"
        assert r.messages[1].text == "您好"
        assert r.messages[0].role == "客"
        assert r.messages[1].role == "助"
        assert r.messages[1].speaker == "助理A"

    def test_block_header_with_timestamp_stripped(self):
        """块头格式：名字/时间戳不入内容，时间戳原样保留。"""
        raw = (
            "客户 哈尔滨赢家1122 2026-05-29 15:30:14\n您在机构圈子里解读的股票我天天看\n\n"
            "助理韩珂龙头班（王萌）\n2026-05-29 17:32:56\n不用客气\n\n"
            "客户哈尔滨赢家1122\n2026-08-24 10:37:34\n上午好，宇树现在跌这么多了，什么价位参与比较好？"
        )
        r = parse_multi(raw)
        m = r.messages
        assert m[0].role == "客" and m[0].speaker == "哈尔滨赢家1122"
        assert m[0].timestamp == "2026-05-29 15:30:14"
        assert "哈尔滨赢家1122" not in m[0].text
        assert m[1].role == "助" and m[1].speaker == "韩珂龙头班（王萌）"
        assert m[1].timestamp == "2026-05-29 17:32:56"
        assert m[1].text == "不用客气"
        assert m[2].timestamp == "2026-08-24 10:37:34"
        assert "哈尔滨赢家1122" not in m[2].text

    def test_line_prefix_timestamp_on_marker(self):
        raw = "2026-08-24 10:23 [客服A] 您好\n2026-08-24 10:24 [客] 在吗"
        r = parse_multi(raw)
        assert r.messages[0].timestamp == "2026-08-24 10:23"
        assert r.messages[0].text == "您好"
        assert r.messages[1].timestamp == "2026-08-24 10:24"

    def test_wechat_duo_line_format(self):
        """微信双行：时间+发送人 行 + 下一行内容。"""
        raw = "2026-08-24 10:23:45 王萌\n您好，您的基金持仓我看了，先别慌。\n2026-08-24 10:25:00 哈尔滨赢家1122\n好的老师"
        r = parse_multi(raw, EMPLOYEES)
        m = r.messages
        assert len(m) == 2
        assert m[0].role == "助" and m[0].speaker == "王萌" and m[0].assistant_id == 1
        assert m[0].timestamp == "2026-08-24 10:23:45"
        assert m[1].role == "客" and m[1].speaker == "哈尔滨赢家1122"

    def test_customer_called_teacher_not_assistant(self):
        """客户昵称含"老师"不误判为助理（推断词表刻意排除"老师"）。"""
        raw = "2026-08-24 10:23:45 王老师\n你好，帮我看看这只票\n2026-08-24 10:25:00 王萌\n好的"
        r = parse_multi(raw, EMPLOYEES)
        assert r.messages[0].role == "客"
        assert r.messages[1].role == "助"

    def test_unknown_speaker_line_colon(self):
        raw = "哈尔滨赢家1122：老师，这票还能拿吗\n王萌：右侧确认后再考虑介入"
        r = parse_multi(raw, EMPLOYEES)
        assert [m.role for m in r.messages] == ["客", "助"]
        assert r.messages[0].text == "老师，这票还能拿吗"

    def test_infer_role_chinese_name(self):
        """中文人名推断：员工名/中文名/班级名 → 助；称谓词/数字昵称 → 客。"""
        from backend.services.multiparser import _infer_role, _looks_like_sender_line

        assert _infer_role("王萌", EMPLOYEES) == "助"  # 员工精确
        assert _infer_role("徐艺桐", EMPLOYEES) == "助"
        assert _infer_role("韩珂龙头班（王萌）", EMPLOYEES) == "助"  # 班级名→王萌
        assert _infer_role("王老师", EMPLOYEES) == "客"  # 客户称呼（称谓词先行拦截）
        assert _infer_role("张先生", EMPLOYEES) == "客"
        assert _infer_role("哈尔滨赢家1122", EMPLOYEES) == "客"  # 数字昵称
        # 三行式发送人行判定：长中文句子/含称谓内容行不得误判
        assert _looks_like_sender_line("我会持续关注", EMPLOYEES) is False
        assert _looks_like_sender_line("好的老师", EMPLOYEES) is False
        assert _looks_like_sender_line("王萌", EMPLOYEES) is True

    def test_empty_raises(self):
        with pytest.raises(ParseError):
            parse_multi("   ")
        with pytest.raises(ParseError):
            parse_multi("完全没有任何角色标记的内容文本")

    def test_three_line_format_last_assistant(self):
        """三行式导出（时间戳行 / 发送人行 / 内容行）：最后一条是助理回复不再被并入上一条客户轮。"""
        raw = (
            "2026-08-24 10:23:45\n王萌\n您好，您的基金持仓我看了，先别慌。\n"
            "2026-08-24 10:25:00\n哈尔滨赢家1122\n好的老师，那我再等等"
        )
        r = parse_multi(raw, EMPLOYEES)
        m = r.messages
        assert len(m) == 2
        assert m[0].role == "助" and m[0].speaker == "王萌" and m[0].assistant_id == 1
        assert m[0].timestamp == "2026-08-24 10:23:45"
        assert m[0].text == "您好，您的基金持仓我看了，先别慌。"
        assert m[1].role == "客" and m[1].speaker == "哈尔滨赢家1122"
        assert m[1].timestamp == "2026-08-24 10:25:00"

    def test_three_line_class_name_matches_employee(self):
        """三行式发送人行是"班级名（姓名）"：canonicalize 后命中员工，时间戳归该轮。"""
        raw = (
            "2026-08-24 10:23:45\n韩珂龙头班（王萌）\n您好，先别慌。\n"
            "2026-08-24 10:25:00\n客户哈尔滨赢家1122\n好的老师"
        )
        r = parse_multi(raw, EMPLOYEES)
        m = r.messages
        assert m[0].role == "助" and m[0].speaker == "韩珂龙头班（王萌）"
        assert m[0].assistant_id == 1 and m[0].timestamp == "2026-08-24 10:23:45"
        assert m[1].role == "客" and m[1].speaker == "哈尔滨赢家1122"

    def test_three_line_two_line_content_not_sender(self):
        """两行式（时间戳行 + 内容行）内容行不得被误判为发送人（"好的老师"含称谓 → 客内容续入）。"""
        raw = "2026-08-24 10:23:45 王萌\n您好\n2026-08-24 10:25:00\n好的老师"
        r = parse_multi(raw, EMPLOYEES)
        m = r.messages
        assert len(m) == 1
        assert m[0].role == "助"
        assert m[0].text == "您好\n好的老师"

    def test_timestamp_milliseconds(self):
        """时间戳毫秒变体：三行式 / 微信双行 / 块头尾缀均完整保留毫秒。"""
        raw = (
            "2026-08-24 10:23:45.123\n王萌\n您好\n"
            "2026-08-24 10:25:00.999 哈尔滨赢家1122\n好的老师，那我再等等\n"
            "客户 王先生 2026-08-24 10:26:01.5\n收到，谢谢"
        )
        r = parse_multi(raw, EMPLOYEES)
        m = r.messages
        assert m[0].timestamp == "2026-08-24 10:23:45.123" and m[0].role == "助"
        assert m[1].timestamp == "2026-08-24 10:25:00.999" and m[1].role == "客"
        assert m[2].timestamp == "2026-08-24 10:26:01.5" and m[2].role == "客"
        assert m[2].speaker == "王先生"

    def test_timestamp_no_year_and_t_sep(self):
        """无年份日期（必须带时间）与 T 分隔：8-24 10:23:45 / 2026-08-24T10:23:45。"""
        raw = (
            "08-24 10:23:45\n王萌\n您好\n"
            "2026-08-24T10:25:00 哈尔滨赢家1122\n好的老师"
        )
        r = parse_multi(raw, EMPLOYEES)
        m = r.messages
        assert m[0].timestamp == "08-24 10:23:45" and m[0].role == "助"
        assert m[1].timestamp == "2026-08-24T10:25:00" and m[1].role == "客"
        # 无年份纯日期不得误当时间戳（"10.25 补仓提醒"是内容行）
        r2 = parse_multi("2026-08-24 10:23:45 王萌\n您好\n10.25 补仓提醒\n2026-08-24 10:26:00\n好的老师")
        assert "10.25 补仓提醒" in r2.messages[0].text

    def test_short_acknowledgment_not_sender(self):
        """短回应词/纯字母数字短行不得误判发送人行；真实昵称/员工/工号不受影响。"""
        from backend.services.multiparser import _looks_like_sender_line

        for w in ("好的", "嗯", "行", "可以", "收到", "谢谢", "再见", "OK", "好", "对", "是", "666", "okay"):
            assert _looks_like_sender_line(w, EMPLOYEES) is False, w
        assert _looks_like_sender_line("哈尔滨赢家1122", EMPLOYEES) is True  # 数字昵称（含中文）
        assert _looks_like_sender_line("zhangsan88", EMPLOYEES) is True  # 长字母数字昵称
        assert _looks_like_sender_line("王萌", EMPLOYEES) is True  # 员工名
        assert _looks_like_sender_line("E001", EMPLOYEES) is True  # 工号（员工命中在前）

    def test_three_line_short_reply_not_new_turn(self):
        """三行式导出中短回应内容行（"好的"）不得被误判为发送人建空轮，续入上一轮。"""
        raw = "2026-08-24 10:23:45\n王萌\n您好\n2026-08-24 10:25:00\n好的"
        r = parse_multi(raw, EMPLOYEES)
        m = r.messages
        assert len(m) == 1, [f"#{x.turn_no} {x.role} {x.speaker}" for x in m]
        assert m[0].role == "助" and m[0].speaker == "王萌"
        assert m[0].text == "您好\n好的"

    def test_wechat_short_ack_timestamp_attached(self):
        """微信双行短回应（"2026-08-24 10:25:00 好的"）：不建新轮，时间戳挂上一轮、回应续入。"""
        raw = "2026-08-24 10:23:45 王萌\n您好\n2026-08-24 10:25:00 好的"
        r = parse_multi(raw, EMPLOYEES)
        m = r.messages
        assert len(m) == 1
        assert m[0].text == "您好\n好的"

    def test_block_header_content_with_colon_appended(self):
        """块头+时间戳后的内容行（含"王萌：帮我看看"、"老师：这个票怎么样"）续入块头轮，不得被劫持成新轮。"""
        db = EMPLOYEES + [Emp(3, "段勇亮", "E003")]
        raw1 = "客户 王先生\n2026-08-24 10:23:45\n王萌：帮我看看这只票"
        r1 = parse_multi(raw1, db)
        assert len(r1.messages) == 1, [f"#{x.turn_no} {x.role} {x.speaker}" for x in r1.messages]
        assert r1.messages[0].role == "客" and r1.messages[0].speaker == "王先生"
        assert r1.messages[0].text == "王萌：帮我看看这只票"
        assert r1.messages[0].timestamp == "2026-08-24 10:23:45"
        raw2 = "助理韩珂龙头班段勇亮\n2026-08-24 10:23:45\n老师：这个票怎么样，还能拿吗"
        r2 = parse_multi(raw2, db)
        assert len(r2.messages) == 1
        assert r2.messages[0].role == "助"
        assert r2.messages[0].text == "老师：这个票怎么样，还能拿吗"

    def test_honorific_suffix_sender_rejected(self):
        """称谓后缀（李姐/张哥/王经理/王老师）是客户称呼，不得推断成助理。"""
        from backend.services.multiparser import _infer_role, _looks_like_sender_line

        for w in ("李姐", "张哥", "王经理", "王老师", "张先生", "李女士"):
            assert _infer_role(w, EMPLOYEES) == "客", w
            assert _looks_like_sender_line(w, EMPLOYEES) is False, w

    def test_marker_word_not_eaten_regression(self):
        """"客气了，有收获就好"不得被吃字成角色词"客"：块头+时间戳后的内容行完整续入该轮。"""
        db = EMPLOYEES + [Emp(3, "段勇亮", "E003")]
        raw = (
            "客服韩珂龙头班段勇亮\n2026-08-26 11:33:13\n"
            "客气了，有收获就好，我们一块加油吧"
        )
        r = parse_multi(raw, db)
        m = r.messages
        assert len(m) == 1, [f"#{x.turn_no} {x.role} {x.speaker}: {x.text}" for x in m]
        assert m[0].role == "助"
        assert m[0].speaker == "韩珂龙头班段勇亮"
        assert m[0].canonical_name == "段勇亮"  # 班级名（姓名）互含匹配员工
        assert m[0].timestamp == "2026-08-26 11:33:13"
        assert m[0].text == "客气了，有收获就好，我们一块加油吧"  # 完整，未吃"客"字


class TestEmployeeMatch:
    def test_match_by_name(self):
        raw = "[客] 在吗\n[王萌] 在的"
        r = parse_multi(raw, EMPLOYEES)
        assert r.messages[1].assistant_id == 1
        assert r.clusters[0].display_name == "王萌"

    def test_match_by_employee_no(self):
        raw = "[客] 在吗\n[E002] 在的"
        r = parse_multi(raw, EMPLOYEES)
        assert r.messages[1].assistant_id == 2

    def test_match_contains_name(self):
        raw = "[客] 在吗\n[王萌老师] 在的"
        r = parse_multi(raw, EMPLOYEES)
        assert r.messages[1].assistant_id == 1

    def test_ambiguous_match_returns_none(self):
        """模糊命中多条（A 包含 B 的名字）→ 宁缺毋滥不猜测。"""
        raw = "[客] 在吗\n[徐] 在的"
        r = parse_multi(raw, EMPLOYEES)
        assert r.messages[1].assistant_id is None

    def test_unmatched_keeps_name_and_warns(self):
        """未匹配员工的中文人名 → 保留为助理候选簇并提示归属（宁缺毋滥，不猜员工）。"""
        raw = "[客] 在吗\n[胡馨月] 在的\n[客] 好"
        r = parse_multi(raw, EMPLOYEES)
        assert r.messages[1].assistant_id is None
        assert any("胡馨月" in w for w in r.warnings)


class TestClustering:
    def test_same_person_aliases_merged(self):
        """同人异名（客服A + 投顾A 同尾部标识符）→ 合并为一簇。"""
        raw = "[客服A] 您好\n[客] 你好\n[投顾A] 我来帮您"
        r = parse_multi(raw)
        assert len(r.clusters) == 1
        c = r.clusters[0]
        assert set(c.aliases) == {"客服A", "投顾A"}
        assert c.reply_turn_nos == [1, 3]

    def test_plain_role_word_numbered(self):
        raw = "[助] 您好\n[客] 你好\n[助] 在的"
        r = parse_multi(raw)
        assert len(r.clusters) == 1
        assert r.clusters[0].canonical_name == "助A"

    def test_two_plain_role_words_two_clusters(self):
        raw = "[客服] 您好\n[客] 你好\n[投顾] 您好\n[客] 在吗"
        r = parse_multi(raw)
        assert len(r.clusters) == 2
        assert [c.canonical_name for c in r.clusters] == ["客服A", "投顾A"]

    def test_single_assistant_session(self):
        raw = "[客] 你好\n[助] 您好，请问有什么可以帮您？\n[客] 基金亏了\n[助] 我来帮您分析"
        r = parse_multi(raw)
        assert len(r.clusters) == 1

    def test_many_assistants(self):
        turns = []
        for i in range(12):
            turns.append(f"[客] 问题{i}")
            turns.append(f"[投顾{chr(65 + i)}] 回复{i}")
        r = parse_multi("\n".join(turns))
        assert len(r.clusters) == 12
        assert all(len(c.reply_turn_nos) == 1 for c in r.clusters)


class TestSegments:
    def test_segment_boundaries(self):
        """客Q1→助A→客F1→助B→客F2：段A=[Q1,A]+F1入上下文；段B=[F1,B]+前文[Q1,A]+后文[F2]入上下文。"""
        raw = "[客] Q1\n[助A] A1\n[客] F1\n[助B] B1\n[客] F2"
        r = parse_multi(raw)
        a, b = r.clusters
        assert [(t.turn_no, t.role) for t in a.segment.turns] == [(1, "客"), (2, "助")]
        assert a.segment.evaluation_context["context_turn_nos"] == [3]  # F1 仅作参考
        assert a.segment.evaluation_context["feedback_turn_nos"] == []  # F1 由助B回答 → 不算 A 未答
        assert [(t.turn_no, t.role) for t in b.segment.turns] == [(3, "客"), (4, "助")]
        # 上下文 = 段外更早前文(1,2) + 块尾后客户轮(5)，均仅作参考不计分
        assert b.segment.evaluation_context["context_turn_nos"] == [1, 2, 5]
        assert b.segment.evaluation_context["feedback_turn_nos"] == [5]

    def test_segment_contains_all_replies_abs_turn_no(self):
        """该助理全部实际回复在同一任务内；轮次号非连续时保留绝对号。"""
        raw = "[客] Q1\n[助A] A1\n[客] F1\n[助B] B1\n[客] F2\n[助A] A2"
        r = parse_multi(raw)
        a = next(c for c in r.clusters if c.canonical_name == "助A")
        assert a.reply_turn_nos == [2, 6]
        # 排除助B 的 4；F1(3) 由助B 回答 → 移出 body 入上下文（归属规则）
        assert [t.turn_no for t in a.segment.turns] == [1, 2, 5, 6]
        assert all(t.turn_no in {1, 2, 5, 6} for t in a.segment.turns)
        assert a.segment.evaluation_context["context_turn_nos"] == [3]
        assert a.segment.evaluation_context["feedback_turn_nos"] == []

    def test_segment_customer_turn_answered_by_other_cluster_not_feedback(self):
        """客户轮被其他助理回答（归属他簇）→ 从本簇 body/feedback 移除，仅入上下文衔接。"""
        raw = "[客] Q1\n[助A] A1\n[客] F1\n[助B] B1"
        r = parse_multi(raw)
        a, b = r.clusters
        assert [(t.turn_no, t.role) for t in a.segment.turns] == [(1, "客"), (2, "助")]
        assert a.segment.evaluation_context["context_turn_nos"] == [3]  # F1 被助B回答 → 上下文
        assert a.segment.evaluation_context["feedback_turn_nos"] == []  # 不再算 A 未答反馈
        assert [(t.turn_no, t.role) for t in b.segment.turns] == [(3, "客"), (4, "助")]  # F1 归 B

    def test_segment_tail_unanswered_keeps_feedback(self):
        """对话末尾无后继助轮的客户轮 → 仍计未答反馈（真正无人回答）。"""
        raw = "[客] Q1\n[助A] A1\n[客] F1\n[助A] A2\n[客] F2"
        r = parse_multi(raw)
        a = r.clusters[0]
        assert [t.turn_no for t in a.segment.turns] == [1, 2, 3, 4]  # F1 后继=A2 → 归 A
        assert a.segment.evaluation_context["feedback_turn_nos"] == [5]  # F2 无后继 → 未答反馈
        assert a.segment.evaluation_context["context_turn_nos"] == [5]

    def test_segment_production_shape_regression(self):
        """生产形态回归（东方国信案例）：客户问被徐艺桐回答 → 只进徐段，段勇亮段不含该问题。"""
        db = EMPLOYEES + [Emp(3, "段勇亮", "E003")]
        raw = "[客] 东方国信还能买回加仓吗\n[徐艺桐] 右侧确认后再考虑介入\n[客] 谢谢\n[段勇亮] 客气了，有收获就好"
        r = parse_multi(raw, db)
        xy = next(c for c in r.clusters if c.canonical_name == "徐艺桐")
        dy = next(c for c in r.clusters if c.canonical_name == "段勇亮")
        assert [t.turn_no for t in xy.segment.turns] == [1, 2]  # 客户问 + 徐的回答
        assert [t.turn_no for t in dy.segment.turns] == [3, 4]  # 客户致谢 + 段回应
        assert 1 not in [t.turn_no for t in dy.segment.turns]  # 东方国信问题不在段勇亮段
        assert dy.segment.evaluation_context["context_turn_nos"] == [1, 2]  # 问题+徐轮仅作上下文
        assert xy.segment.evaluation_context["feedback_turn_nos"] == []

    def test_pre_context_backtracking(self):
        """段首回溯：紧邻连续客户轮入段；跨助轮找最近客户轮。"""
        raw = "[客] Q1\n[客] Q2\n[助A] A1\n[客] F1\n[助B] B1"
        r = parse_multi(raw)
        a = next(c for c in r.clusters if c.canonical_name == "助A")
        assert [t.turn_no for t in a.segment.turns] == [1, 2, 3]

    def test_pre_context_cap(self):
        raw = "\n".join([f"[客] Q{i}" for i in range(15)] + ["[助A] 回复", "[客] 好"])
        r = parse_multi(raw)
        a = r.clusters[0]
        assert len([t for t in a.segment.turns if t.role == "客"]) <= 11  # 10 轮上限 + 1
        assert any("截断" in w for w in r.warnings)

    def test_evaluation_material_parseable(self):
        """段文本可被 parse_turns 回读且 speaker 不被改写（绝对轮次号保留）。"""
        raw = "[客] Q1\n[助A] A1\n[客] F1\n[助B] B1"
        r = parse_multi(raw)
        for c in r.clusters:
            p = parse_turns(c.segment.turns)
            assert [t.turn_no for t in p.turns] == [t.turn_no for t in c.segment.turns]
            assert all(t.speaker == t.speaker for t in p.turns)


class TestCsvJson:
    def test_csv_with_time_column(self):
        raw = "发送人,内容,时间\n客户,你好,2026-08-24 10:00\n王萌,您好,2026-08-24 10:01"
        r = parse_multi(raw, EMPLOYEES)
        assert r.fmt == "csv"
        assert r.messages[0].timestamp == "2026-08-24 10:00"
        assert r.messages[1].assistant_id == 1
        assert r.messages[1].timestamp == "2026-08-24 10:01"

    def test_json_with_time_field(self):
        raw = '[{"role": "客", "content": "你好", "time": "2026-08-24 10:00"}, {"role": "助", "content": "您好", "time": "2026-08-24 10:01"}]'
        r = parse_multi(raw)
        assert r.fmt == "json"
        assert r.messages[0].timestamp == "2026-08-24 10:00"
        assert r.messages[1].role == "助"
