# 批量会话切分：按客户昵称变化切分（纯规则，不调 LLM）；兜底单任务；解析失败不 400
import json

from backend.services.batch.splitter import dict_to_message, message_to_dict, split_customers


class Emp:
    def __init__(self, id, name, employee_no):
        self.id, self.name, self.employee_no = id, name, employee_no


EMPLOYEES = [Emp(1, "王萌", "E001"), Emp(2, "徐艺桐", "E002"), Emp(3, "段勇亮", "E003")]
NAME_MAP = {"韩珂龙头班": "段勇亮"}

THREE_CUSTOMERS = (
    "邯郸赢家0878\n2026-07-03 13:12:42\n你好韩老师！300166提醒加仓没看到现在能加吗？\n\n"
    "韩珂龙头班\n2026-07-03 14:32:24\n可以按照中线模式低吸加仓5%，不要追涨就好\n\n"
    "昆明赢家2735\n2026-08-25 21:22:22\n韩老师麻烦问下京东方a明天可以买吗？\n\n"
    "山人俱乐部（李金潓）\n2026-08-25 22:51:19\n可以，中长线没问题\n\n"
    "深圳散户888\n2026-08-28 09:00:00\n老师，帮我看看三一重工\n\n"
    "韩珂龙头班\n2026-08-28 09:05:00\n好的，稍等我看下\n"
)


class TestSplitCustomers:
    def test_three_customers_split(self):
        """块头文本三个客户 → 3 个会话；同客户多助理轮替保留在同一会话。"""
        out = split_customers(THREE_CUSTOMERS, EMPLOYEES, NAME_MAP)
        assert out["task_count"] == 3
        assert out["message_count"] == 6
        assert out["assistant_count"] == 2  # 韩珂龙头班（段勇亮）+ 山人俱乐部（李金潓）
        names = [c.customer_name for c in out["customers"]]
        assert names == ["邯郸赢家0878", "昆明赢家2735", "深圳散户888"]
        assert [c.message_count for c in out["customers"]] == [2, 2, 2]
        # 每会话参与助理（去重按出现顺序）
        assert out["customers"][0].assistant_names == ["段勇亮"]
        assert out["customers"][1].assistant_names == ["李金潓"]
        assert out["customers"][2].assistant_names == ["段勇亮"]

    def test_customer_turn_no_preserved(self):
        """切分后的消息保留原始绝对 turn_no（跨会话连续编号，断点续跑/高亮可溯源）。"""
        out = split_customers(THREE_CUSTOMERS, EMPLOYEES, NAME_MAP)
        all_no = [m.turn_no for c in out["customers"] for m in c.messages]
        assert all_no == list(range(1, 7))
        assert out["customers"][1].messages[0].text == "韩老师麻烦问下京东方a明天可以买吗？"

    def test_marker_format_single_task_fallback(self):
        """纯 marker 格式（客户恒为「客户」）→ 整段单任务 + warning。"""
        raw = "【客户】你好\n【助理A】您好\n【客户】谢谢\n【助理A】不客气\n"
        out = split_customers(raw, EMPLOYEES)
        assert out["task_count"] == 1
        assert out["customers"][0].customer_name == "客户"
        assert out["customers"][0].message_count == 4
        assert any("未按客户昵称切分" in w for w in out["warnings"])

    def test_csv_single_task(self):
        """CSV 格式角色列无法区分客户 → 整段单任务。"""
        raw = "角色,内容\n客,你好\n助,您好\n客,谢谢\n助,不客气\n"
        out = split_customers(raw, EMPLOYEES)
        assert out["task_count"] == 1
        assert out["customers"][0].message_count == 4

    def test_unparseable_not_400(self):
        """解析失败不抛错：parse_error 非空、task_count=1（执行时失败显示解析错误）。"""
        out = split_customers("这是一段无法识别的普通文本", EMPLOYEES)
        assert out["parse_error"]
        assert out["task_count"] == 1
        assert out["customers"] == []

    def test_no_customer_messages_fallback(self):
        """无客户消息（全是助理轮）→ 整段单任务兜底。"""
        raw = "【助理A】您好\n【助理B】我来帮您查\n"
        out = split_customers(raw, EMPLOYEES)
        assert out["task_count"] == 1
        assert out["customers"][0].message_count == 2
        assert out["customers"][0].assistant_names == ["助理A", "助理B"]

    def test_message_roundtrip(self):
        """message_to_dict / dict_to_message 往返无损（input_data 事实源）。"""
        out = split_customers(THREE_CUSTOMERS, EMPLOYEES, NAME_MAP)
        for c in out["customers"]:
            for m in c.messages:
                back = dict_to_message(message_to_dict(m))
                assert back.turn_no == m.turn_no
                assert back.role == m.role
                assert back.speaker == m.speaker
                assert back.canonical_name == m.canonical_name
                assert back.text == m.text
                assert back.timestamp == m.timestamp

    def test_input_data_json_serializable(self):
        """切分结果可直接序列化为 input_data（import 端点落库用）。"""
        out = split_customers(THREE_CUSTOMERS, EMPLOYEES, NAME_MAP)
        payload = json.dumps([message_to_dict(m) for m in out["customers"][0].messages], ensure_ascii=False)
        assert "邯郸赢家0878" in payload


# Excel「房间对话」导出一行：完整聊天记录用【时间】发送人（角色）标注 ——
# 真实文件「聊天记录.xlsx」第 11 列格式（房间 1395619 抽样）。
ROOM_CAO = """【2026-08-19 17:35:44】曹*（客户）
大鹏老师好， 请教一下，隆基绿能可以买吗？

【2026-08-19 21:03:10】曹瑞格（投顾助理；是否目标助理：是；是否代理老师发言：是；客户看到：大鹏寻龙班）
隆基绿能目前还在关注，持仓周期会长一些

【2026-08-20 08:44:58】曹*（客户）
收到，谢谢老师

【2026-08-20 10:19:44】温静（投顾助理；是否目标助理：否；是否代理老师发言：是）
没事的"""

ROOM_DING = """【2026-08-21 09:12:33】丁*（客户）
老师，黄金还能追吗？

【2026-08-21 09:30:10】曹瑞格（投顾助理；是否目标助理：是；是否代理老师发言：是）
黄金短线位置偏高，建议等回调"""


class TestSplitRooms:
    """rooms 模式：Excel 房间导出 → 每个房间独立会话（【】时间戳归一化 + 角色去掉）。"""

    def test_single_room_parsed(self):
        out = split_customers(None, EMPLOYEES, rooms=[{"customer_name": "曹*（1395619）", "text": ROOM_CAO}])
        assert out["task_count"] == 1
        assert out["message_count"] == 4
        assert out["customers"][0].customer_name == "曹*（1395619）"
        assert out["customers"][0].assistant_names == ["曹瑞格", "温静"]
        msgs = out["customers"][0].messages
        assert [(m.role, m.speaker) for m in msgs] == [
            ("客", "曹*"), ("助", "曹瑞格"), ("客", "曹*"), ("助", "温静"),
        ]
        assert msgs[0].timestamp == "2026-08-19 17:35:44"
        # 内容 100% 原样保留（只改发送人行，不动对话文本）
        assert msgs[0].text == "大鹏老师好， 请教一下，隆基绿能可以买吗？"
        assert msgs[2].text == "收到，谢谢老师"

    def test_multi_rooms_independent(self):
        out = split_customers(
            None,
            EMPLOYEES,
            rooms=[
                {"customer_name": "曹*（1395619）", "text": ROOM_CAO},
                {"customer_name": "丁*（1396116）", "text": ROOM_DING},
            ],
        )
        assert out["task_count"] == 2
        assert out["message_count"] == 6
        names = [c.customer_name for c in out["customers"]]
        assert names == ["曹*（1395619）", "丁*（1396116）"]
        # 各自 turn_no 从 1 重新编号（批内任务独立会话）
        assert [m.turn_no for m in out["customers"][1].messages] == [1, 2]

    def test_empty_room_skipped_with_warning(self):
        out = split_customers(
            None,
            EMPLOYEES,
            rooms=[
                {"customer_name": "曹*（1395619）", "text": ROOM_CAO},
                {"customer_name": "空房间", "text": "【2026-08-21 09:12:33】曹瑞格（投顾助理）\n仅助理发言"},
            ],
        )
        assert out["task_count"] == 1
        assert any("空房间" in w for w in out["warnings"])

    def test_rooms_take_precedence_over_raw_text(self):
        """rooms 非空时忽略 raw_text（API 传房间即按房间处理）。"""
        out = split_customers(THREE_CUSTOMERS, EMPLOYEES, NAME_MAP, rooms=[{"customer_name": "曹*", "text": ROOM_CAO}])
        assert out["task_count"] == 1
        assert out["customers"][0].customer_name == "曹*"

    def test_room_text_preserved_verbatim(self):
        """归一化只改发送人行；对话文本（含多行内容）原样保留，不截断不转义。"""
        room = """【2026-08-19 17:35:44】曹*（客户）
第一行内容

第二段内容，中间有空行

第三行含特殊字符 $%^&*() 你好"""
        out = split_customers(None, EMPLOYEES, rooms=[{"customer_name": "曹*", "text": room}])
        assert out["message_count"] == 1
        # 消息体内部空行按系统既有解析语义折叠；文字内容原样保留
        assert out["customers"][0].messages[0].text == "第一行内容\n第二段内容，中间有空行\n第三行含特殊字符 $%^&*() 你好"
