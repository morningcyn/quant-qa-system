# 「自动识别并整理」：粘贴原始记录 → 【客户】【助理A】标签文本（识别复用 multiparser，纯规则）
import pytest
from fastapi.testclient import TestClient

from backend.db.database import get_db
from backend.main import app
from backend.services import multiparser, organizer
from backend.utils.errors import BizError


class Emp:
    """测试用员工替身（鸭子类型：id/name/employee_no）。"""

    def __init__(self, id, name, employee_no):
        self.id, self.name, self.employee_no = id, name, employee_no


EMPLOYEES = [Emp(1, "王萌", "E001"), Emp(2, "徐艺桐", "E002")]
DB_WITH_DYL = EMPLOYEES + [Emp(3, "段勇亮", "E003")]

THREE_LINE = (
    "邯郸赢家0878\n2026-07-03 13:12:42\n你好韩老师！300166提醒加仓没看到现在能加吗？\n\n"
    "韩珂龙头班\n2026-07-03 14:32:24\n可以按照中线模式低吸加仓5%，不要追涨就好\n\n"
    "邯郸赢家0878\n2026-07-10 13:31:50\n韩老师好！000420现在能加仓吗？"
)


class TestOrganizeText:
    def test_three_line_single_assistant(self):
        """三行式单助理 → 【客户】×2 + 【助理A】×1，内容逐字不变。"""
        out = organizer.organize_text(THREE_LINE, DB_WITH_DYL, {"韩珂龙头班": "段勇亮"})
        assert out["message_count"] == 3
        assert out["role_stats"] == {"客": 2, "助": 1, "total": 3}
        assert out["organized_text"] == (
            "【客户】你好韩老师！300166提醒加仓没看到现在能加吗？\n"
            "【助理A】可以按照中线模式低吸加仓5%，不要追涨就好\n"
            "【客户】韩老师好！000420现在能加仓吗？"
        )
        # 标签命名参考：助理A = 段勇亮（员工匹配 + name_map 兜底）
        assert out["assistants"] == [{"canonical_name": "段勇亮", "label": "助理A"}]

    def test_two_assistants_numbered_by_first_seen(self):
        """两个不同显示名助理 → 按消息首现顺序编 A/B。"""
        raw = (
            "昆明赢家2735\n2026-08-25 21:22:22\n韩老师麻烦问下京东方a明天可以买吗？\n\n"
            "韩珂龙头班\n2026-08-25 22:51:19\n可以，中长线没问题\n\n"
            "昆明赢家2735\n2026-08-25 22:52:00\n好的谢谢老师\n\n"
            "峰哥荐股\n2026-08-25 23:00:00\n我也补充一句，短线注意仓位"
        )
        out = organizer.organize_text(raw, DB_WITH_DYL, {"韩珂龙头班": "段勇亮"})
        lines = out["organized_text"].split("\n")
        assert lines[0].startswith("【客户】")
        assert lines[1].startswith("【助理A】")  # 韩珂龙头班段勇亮（name_map）先出现
        assert lines[2].startswith("【客户】")
        assert lines[3].startswith("【助理B】")  # 峰哥荐股 后出现
        assert out["assistants"] == [
            {"canonical_name": "段勇亮", "label": "助理A"},
            {"canonical_name": "峰哥荐股", "label": "助理B"},
        ]

    def test_name_map_real_name_in_cluster(self):
        """name_map 兜底：韩珂龙头班 → 员工段勇亮（与 preview/dispatcher 同一映射）。"""
        out = organizer.organize_text(THREE_LINE, DB_WITH_DYL, {"韩珂龙头班": "段勇亮"})
        assert out["assistants"][0]["canonical_name"] == "段勇亮"
        # 无 name_map → 未匹配员工，簇为显示名本身
        out2 = organizer.organize_text(THREE_LINE, DB_WITH_DYL)
        assert out2["assistants"][0]["canonical_name"] == "韩珂龙头班"

    def test_multiline_content_preserved(self):
        """多行内容消息 → 全部内容行保留（重新解析时由内容续行机制并回）。"""
        raw = (
            "邯郸赢家0878\n2026-07-03 13:12:42\n你好韩老师！\n300166提醒加仓没看到\n现在能加吗？\n\n"
            "韩珂龙头班\n2026-07-03 14:32:24\n可以\n按照中线模式低吸加仓5%"
        )
        out = organizer.organize_text(raw, DB_WITH_DYL, {"韩珂龙头班": "段勇亮"})
        assert out["organized_text"] == (
            "【客户】你好韩老师！\n300166提醒加仓没看到\n现在能加吗？\n"
            "【助理A】可以\n按照中线模式低吸加仓5%"
        )

    def test_roundtrip_organize_then_parse(self):
        """round-trip：整理后文本再进 parse_multi → 轮次/角色/内容与整理前一致。"""
        before = multiparser.parse_multi(THREE_LINE, DB_WITH_DYL, {"韩珂龙头班": "段勇亮"})
        out = organizer.organize_text(THREE_LINE, DB_WITH_DYL, {"韩珂龙头班": "段勇亮"})
        after = multiparser.parse_multi(out["organized_text"], DB_WITH_DYL)
        assert len(after.messages) == len(before.messages) == 3
        assert [m.role for m in after.messages] == [m.role for m in before.messages]
        assert [m.text for m in after.messages] == [m.text for m in before.messages]
        # 整理后为纯标签格式：助理消息 speaker=助理A（簇名未匹配员工，供预览页人工归属）
        assert after.messages[1].speaker == "助理A"

    def test_manual_real_name_split_same_display_name(self):
        """同名显示名助理的手动整理路径：把【助理X】标签改成真实姓名 → 分别成簇 + 员工匹配。

        用户业务：一个客户由多位助理共同回复，但复制出的纯文本里显示名相同（如都是"韩珂龙头班"），
        无法自动区分。用户把部分【助理A】改成【段勇亮】【徐艺桐】等真实姓名标签后，
        解析器经 ⑤b 方括号裸名 + 员工匹配自动成簇并归属。"""
        tagged = (
            "【客户】韩老师科创板创业板这天天跌不停\n"
            "【段勇亮】目前呢整体市场情绪都是不好的，不要着急\n"
            "【徐艺桐】放心，老师会一直陪着你的\n"
            "【客户】谢谢韩老师\n"
            "【段勇亮】客气了，有问题随时沟通交流就好\n"
        )
        r = multiparser.parse_multi(tagged, DB_WITH_DYL)
        assert [c.canonical_name for c in r.clusters] == ["段勇亮", "徐艺桐"]
        assert r.clusters[0].assistant_id == 3 and r.clusters[1].assistant_id == 2
        # 同一人（段勇亮）的两条消息自动合并为一簇
        assert r.clusters[0].reply_turn_nos == [2, 5]
        # 组织输出幂等：真实姓名标签再整理仍保持独立簇
        out = organizer.organize_text(tagged, DB_WITH_DYL)
        assert [a["label"] for a in out["assistants"]] == ["助理A", "助理B"]
        assert [a["canonical_name"] for a in out["assistants"]] == ["段勇亮", "徐艺桐"]

    def test_empty_label_line_next_content(self):
        """空内容标签行（【段勇亮】独占一行、内容在下一行——手动改名常出现）→ 消息不丢、内容并回。"""
        tagged = (
            "【客户】韩老师科创板这天天跌\n"
            "【段勇亮】\n"
            "目前呢整体市场情绪不好，不要着急\n"
            "【徐艺桐】放心，老师会一直陪着你的\n"
            "【客户】谢谢韩老师\n"
            "【段勇亮】\n"
            "客气了\n"
        )
        r = multiparser.parse_multi(tagged, DB_WITH_DYL)
        assert [m.text for m in r.messages] == [
            "韩老师科创板这天天跌",
            "目前呢整体市场情绪不好，不要着急",  # 空标签行的内容不吞并、不消失
            "放心，老师会一直陪着你的",
            "谢谢韩老师",
            "客气了",
        ]
        assert [c.canonical_name for c in r.clusters] == ["段勇亮", "徐艺桐"]
        assert r.clusters[0].assistant_id == 3
        assert r.clusters[0].reply_turn_nos == [2, 5]

    def test_empty_label_line_next_label_not_swallowed(self):
        """空内容标签行后紧接另一条标签行 → 空标签轮落库保留归属，不吞并下一条。"""
        tagged = (
            "【客户】韩老师科创板这天天跌\n"
            "【段勇亮】\n"
            "【徐艺桐】放心，老师会一直陪着你的\n"
        )
        r = multiparser.parse_multi(tagged, DB_WITH_DYL)
        assert [m.role for m in r.messages] == ["客", "助", "助"]
        assert [m.text for m in r.messages] == ["韩老师科创板这天天跌", "", "放心，老师会一直陪着你的"]
        assert [c.canonical_name for c in r.clusters] == ["段勇亮", "徐艺桐"]
        assert r.clusters[0].assistant_id == 3 and r.clusters[1].assistant_id == 2

    def test_marker_input_organize(self):
        """marker 输入（[客]/[助理A]）也能整理（多格式兼容）。"""
        raw = "[客] 你好\n[助理A] 您好\n[客] 基金亏了\n[助理B] 我来帮您查"
        out = organizer.organize_text(raw, EMPLOYEES)
        assert out["organized_text"] == "【客户】你好\n【助理A】您好\n【客户】基金亏了\n【助理B】我来帮您查"

    def test_empty_input_raises(self):
        with pytest.raises(BizError) as excinfo:
            organizer.organize_text("   ", EMPLOYEES)
        assert excinfo.value.code == "organize_failed"

    def test_unparseable_input_raises(self):
        with pytest.raises(BizError) as excinfo:
            organizer.organize_text("这是一段无法识别的普通文本", EMPLOYEES)
        assert excinfo.value.code == "organize_failed"
        assert excinfo.value.status_code == 400


class TestOrganizeApi:
    @pytest.fixture()
    def client(self, session):
        def override_db():
            yield session

        app.dependency_overrides[get_db] = override_db
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()

    def test_organize_endpoint(self, client, session):
        from backend.db import repository

        repository.create_assistant(session, "段勇亮", "E003", "standard")
        resp = client.post("/api/parse/organize", json={"raw_text": THREE_LINE})
        assert resp.status_code == 200
        data = resp.json()
        assert data["message_count"] == 3
        assert data["organized_text"].count("【客户】") == 2
        assert "【助理A】" in data["organized_text"]
        assert data["assistants"][0]["canonical_name"] == "段勇亮"  # 生产库员工匹配

    def test_organize_endpoint_unparseable(self, client):
        resp = client.post("/api/parse/organize", json={"raw_text": "无法识别的普通文本"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "organize_failed"
