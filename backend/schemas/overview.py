# 多人质检总览：LLM 一次汇总输出协议（LLM 失败时规则降级，字段可缺省）
from typing import Literal

from pydantic import BaseModel, Field


class OverviewResult(BaseModel):
    """本次客户服务总览（LLM 汇总输出）。

    degraded 场景下（LLM 失败走规则降级）仍用同一结构，保证前端渲染一致。
    """

    model_config = {"extra": "ignore"}

    main_strengths: list[str] = Field(default_factory=list)
    main_issues: list[str] = Field(default_factory=list)
    customer_issue_resolved: Literal["是", "部分", "否", "无法判断"] = "无法判断"
    resolution_reason: str = Field(default="")
    overall_comment: str = Field(default="")
