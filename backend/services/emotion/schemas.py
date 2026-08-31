# 客户情绪识别：LLM 输出契约（仅模块内部使用，不进 backend/schemas/）
from typing import Literal

from pydantic import BaseModel, Field

# 8 类情绪（用户第一版分类；无明显情绪 = 中性，不强行判断）
EMOTION_TYPES = Literal["积极/认可", "中性", "担忧", "焦虑", "不满", "愤怒", "失望", "怀疑"]

# 触发原因（证券咨询场景；无法确定时用 其他/未知，不强行分类）
TRIGGER_TYPES = Literal[
    "持仓亏损",
    "行情波动",
    "错过机会",
    "策略质疑",
    "服务等待",
    "收益期待",
    "仓位压力",
    "市场恐慌",
    "其他",
    "未知",
]


class EmotionItem(BaseModel):
    """一条客户消息的情绪标注。"""

    model_config = {"extra": "ignore"}  # 容忍 LLM 多余字段

    turn_no: int = Field(ge=1)
    emotion: EMOTION_TYPES
    intensity: int = Field(ge=0, le=5)
    confidence: float = Field(ge=0.0, le=1.0)
    trigger: TRIGGER_TYPES
    evidence: str = Field(min_length=1)  # 必须引用客户原话（analyzer 再做机器级逐字校验）


class EmotionResult(BaseModel):
    """一次调用的输出：该批全部客户消息的情绪标注。"""

    model_config = {"extra": "ignore"}

    items: list[EmotionItem]
