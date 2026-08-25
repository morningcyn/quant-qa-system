import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config import DEFAULT_TEMPLATES
from backend.db import repository
from backend.db.models import Base
from backend.services.llm.base import BaseLLMClient, LLMError


@pytest.fixture()
def session():
    """独立内存 SQLite + seed 三套模板。

    StaticPool 共享单连接：TestClient 的 worker 线程与主线程看到同一个库，
    避免 SingletonThreadPool 在跨线程时各开一个空内存库。
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as s:
        for ttype, config in DEFAULT_TEMPLATES.items():
            repository.upsert_template(s, ttype, config["name"], config)
        yield s
    engine.dispose()


class MockLLMClient(BaseLLMClient):
    """按队列依次返回响应；响应可为 str（原文）或 Exception（抛出）。"""

    supports_json_mode = True

    def __init__(self, responses: list):
        super().__init__("mock-model")
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def complete(self, system, user, *, temperature=0.2, json_mode=False, max_tokens=8192):
        self.calls.append({"system": system, "user": user, "temperature": temperature})
        if not self.responses:
            raise LLMError("bad_json", "mock: 无更多响应")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def valid_llm_json(total=69, yellow=False, reasons=None, red=False, red_reasons=None) -> str:
    """构造一份与 v2 schema 一致的合法输出（子项对象化 + analysis 思维链 + 红灯字段）。"""
    return json.dumps(
        {
            "total_score": total,
            "is_red_alert": red,
            "red_alert_reasons": red_reasons or [],
            "is_yellow_alert": yellow,
            "yellow_alert_reasons": reasons or [],
            "d_scores": {
                "d1_emotion_change": {"analysis": "客户由焦虑转为平复", "score": 8, "rating": "轻微变好", "comment": "情绪有所平复。"},
                "d2_profile_match": {"analysis": "识别出套牢恐慌", "profile": "焦虑型", "score": 12, "match_rating": "基本匹配", "comment": "识别出焦虑。"},
                "d3_problem_match": {"analysis": "捕捉资金安全诉求", "score": 9, "surface_vs_deep": "看懂部分", "resolution": "部分匹配", "comment": "方案落地性稍弱。"},
                "d4_expectation_exceed": {"analysis": "预判解套周期", "score": 7, "derived_question": 1, "control_given": 1, "comment": "未给备选方案。"},
            },
            "s_scores": {
                "s1_emotion_stabilize": {"sub_items": {"empathy": {"analysis": "有承接", "score": 4}, "customized": {"analysis": "针对持仓", "score": 4}, "direct": {"analysis": "正面回应", "score": 3}, "no_conflict": {"analysis": "未争辩", "score": 4}, "vent_guide": {"analysis": "未引导倾诉", "score": 0}}},
                "s2_problem_closure": {"sub_items": {"completeness": {"analysis": "全部回应", "score": 3}, "structure": {"analysis": "三段式", "score": 3}, "next_step": {"analysis": "给了下一步", "score": 2}, "follow_up": {"analysis": "承诺跟进", "score": 3}}},
                "s3_professional_supply": {"sub_items": {"logic": {"analysis": "逻辑通顺", "score": 3}, "explain_why": {"analysis": "讲了原因", "score": 2}, "decision_ownership": {"analysis": "交还决策", "score": 2}}},
            },
            "highlight_dialogue": [
                {
                    "turn": 2,
                    "role": "助",
                    "original_text": "这个我们也没办法。",
                    "issue_type": "共情生硬 (S1-1, S2-3)",
                    "ai_rewrite": "我理解您的着急……",
                }
            ],
            "improvement_suggestions": ["先承接情绪。", "用三段论给建议。"],
        },
        ensure_ascii=False,
    )


def low_llm_json() -> str:
    """低分输出：总分应 < 59 触发黄灯。"""
    data = json.loads(valid_llm_json())
    data["total_score"] = 40
    for key, val in data["d_scores"].items():
        val["score"] = 5 if key == "d1_emotion_change" else 4
    data["d_scores"]["d1_emotion_change"]["score"] = 5
    for key, val in data["s_scores"].items():
        val.pop("score", None)  # 算术剥离：不输出维度分，验证后端按子项求和
        for sub in val["sub_items"]:
            val["sub_items"][sub] = {"analysis": "事实盘点", "score": 1}
    return json.dumps(data, ensure_ascii=False)


SAMPLE_DIALOGUE = """[客] 你好，我买的基金最近一直在跌，现在很慌，怎么办？
[助] 您好，理解您的心情。我们看下您持有的产品。
[客] 已经亏了快20个点了，天天睡不着觉。
[助] 这个我们也没办法，行情就这样，建议您拿着别看盘了。
[客] 那要等到什么时候才能回本？
[助] 市场短期波动是正常的，您可以先不动，我会持续帮您关注。
[客] 好的，那后面有情况麻烦您告诉我。
[助] 没问题，我每周给您同步一次持仓情况，有异动第一时间联系您。
"""
