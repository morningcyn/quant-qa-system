# 注入合成质检数据用于前端渲染验证（验证后由 --clean 清理）
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import DEFAULT_TEMPLATES
from backend.db import repository
from backend.db.database import SessionLocal, init_db
from backend.services import scoring
from backend.schemas.inspection import LLMResultSchema

VALID_JSON = {
    "total_score": 69,
    "is_yellow_alert": False,
    "yellow_alert_reasons": [],
    "d_scores": {
        "d1_emotion_change": {"score": 8, "rating": "轻微变好", "comment": "客户开场较焦虑，收尾情绪有所平复。"},
        "d2_profile_match": {"profile": "焦虑型", "score": 12, "match_rating": "基本匹配", "comment": "识别出套牢恐慌，但安抚方式略生硬。"},
        "d3_problem_match": {"score": 9, "surface_vs_deep": "看懂部分", "resolution": "部分匹配", "comment": "捕捉到资金安全诉求，方案落地性稍弱。"},
        "d4_expectation_exceed": {"score": 7, "derived_question": 1, "control_given": 1, "comment": "预判了解套周期，但未给备选方案。"},
    },
    "s_scores": {
        "s1_emotion_stabilize": {"score": 15, "sub_items": {"empathy": 4, "customized": 4, "direct": 3, "no_conflict": 4, "vent_guide": 0}},
        "s2_problem_closure": {"score": 11, "sub_items": {"completeness": 3, "structure": 3, "next_step": 2, "follow_up": 3}},
        "s3_professional_supply": {"score": 7, "sub_items": {"logic": 3, "explain_why": 2, "decision_ownership": 2}},
    },
    "highlight_dialogue": [
        {
            "turn": 4,
            "role": "助",
            "original_text": "这个我们也没办法，行情就这样，建议您拿着别看盘了。",
            "issue_type": "共情生硬 / 未给清晰动作 (S1-1, S2-3)",
            "ai_rewrite": "哥，我特别理解您现在的着急，市场连续震荡确实很考验心态。咱们先看这只票的基本面并未受损，建议仓位上先不动，明早 9:30 开盘我帮您盯紧分时做 T 摊薄成本，您看这样行吗？",
        },
        {
            "turn": 6,
            "role": "助",
            "original_text": "这个要看市场情况，具体时间不好说。",
            "issue_type": "未给时间节点 (S2-3)",
            "ai_rewrite": "我没办法保证具体时点，但我可以给您一个判断框架：先看两周的板块资金流向，我每周三给您复盘一次，一旦出现企稳信号我们第一时间讨论加仓还是减仓，您看可以吗？",
        },
    ],
    "improvement_suggestions": [
        "在客户焦虑发泄时，先做情绪承接，切忌用'我也没办法'等推卸式词汇。",
        "给出建议时遵循【结论 + 原因 + 下一步动作】三段论，明确责任归属。",
    ],
}

LOW_JSON = {
    **VALID_JSON,
    "is_yellow_alert": True,
    "yellow_alert_reasons": ["S1 情绪维稳失分严重（4/20 分）", "D3 诉求穿透只停留在表面（4/15 分）", "S2 问题闭环缺失下一步动作（3/15 分）"],
    "total_score": 44,
    "d_scores": {
        "d1_emotion_change": {"score": 4, "rating": "轻微恶化", "comment": "客户情绪不降反升。"},
        "d2_profile_match": {"profile": "焦虑型", "score": 6, "match_rating": "部分匹配", "comment": "识别了焦虑但未做安抚。"},
        "d3_problem_match": {"score": 4, "surface_vs_deep": "只看表面", "resolution": "未匹配", "comment": "只回应了字面问题。"},
        "d4_expectation_exceed": {"score": 3, "derived_question": 0, "control_given": 0, "comment": "未给任何下一步动作。"},
    },
    "s_scores": {
        "s1_emotion_stabilize": {"score": 4, "sub_items": {"empathy": 1, "customized": 1, "direct": 1, "no_conflict": 1, "vent_guide": 0}},
        "s2_problem_closure": {"score": 3, "sub_items": {"completeness": 1, "structure": 1, "next_step": 0, "follow_up": 1}},
        "s3_professional_supply": {"score": 2, "sub_items": {"logic": 1, "explain_why": 1, "decision_ownership": 0}},
    },
}

MID_JSON = {
    **VALID_JSON,
    "total_score": 66,
    "d_scores": {
        "d1_emotion_change": {"score": 7, "rating": "基本持平", "comment": "情绪略有安抚。"},
        "d2_profile_match": {"profile": "理性型", "score": 11, "match_rating": "基本匹配", "comment": "应对基本得当。"},
        "d3_problem_match": {"score": 8, "surface_vs_deep": "看懂部分", "resolution": "部分匹配", "comment": "理解了一部分深层诉求。"},
        "d4_expectation_exceed": {"score": 6, "derived_question": 1, "control_given": 0, "comment": "预判了问题但未交还掌控感。"},
    },
    "s_scores": {
        "s1_emotion_stabilize": {"score": 14, "sub_items": {"empathy": 3, "customized": 4, "direct": 3, "no_conflict": 4, "vent_guide": 0}},
        "s2_problem_closure": {"score": 11, "sub_items": {"completeness": 3, "structure": 3, "next_step": 2, "follow_up": 3}},
        "s3_professional_supply": {"score": 9, "sub_items": {"logic": 4, "explain_why": 3, "decision_ownership": 2}},
    },
}

RAW = (Path(__file__).resolve().parent.parent / "samples" / "sample_low.txt").read_text(encoding="utf-8")


def seed() -> None:
    init_db()
    with SessionLocal() as session:
        assistant = repository.get_assistant_by_no(session, "DEMO01") or repository.create_assistant(
            session, "演示员工", "DEMO01", "standard"
        )
        template = DEFAULT_TEMPLATES["standard"]
        now = datetime.now()
        for i, payload in enumerate([LOW_JSON, MID_JSON, VALID_JSON]):
            result = LLMResultSchema.model_validate(payload)
            result = scoring.apply_guardrails(result, template, 12)
            inspection = repository.save_inspection(
                session,
                assistant_id=assistant.id,
                session_title=["王先生套牢安抚（差）", "李女士定投咨询（中）", "张先生持仓复盘（良）"][i],
                total_score=result.total_score,
                is_yellow_alert=result.is_yellow_alert,
                yellow_alert_reasons=result.yellow_alert_reasons,
                template_type="standard",
                template_snapshot=template,
                turn_count=12,
                customer_profile=result.d_scores.d2_profile_match.profile or None,
                raw_dialogue=RAW,
                d_scores=result.d_scores.model_dump(),
                s_scores=result.s_scores.model_dump(),
                highlight_dialogue=[h.model_dump() for h in result.highlight_dialogue],
                suggestions=result.improvement_suggestions,
            )
            inspection.created_at = now - timedelta(days=i)
            session.commit()
            print(f"[OK] inspection id={inspection.id} score={result.total_score} yellow={result.is_yellow_alert}")
        print(f"[OK] assistant id={assistant.id}")


def clean() -> None:
    with SessionLocal() as session:
        assistant = repository.get_assistant_by_no(session, "DEMO01")
        if assistant:
            repository.delete_assistant(session, assistant)
            print("[OK] 演示数据已清理")


if __name__ == "__main__":
    if "--clean" in sys.argv:
        clean()
    else:
        seed()
