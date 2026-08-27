# API 请求模型
from pydantic import BaseModel, Field


class AssistantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    employee_no: str = Field(min_length=1, max_length=50)
    template_type: str = "standard"
    teacher_persona: str = Field(default="", max_length=2000, description="该员工扮演的投顾老师人设")


class AssistantUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    template_type: str = "standard"
    teacher_persona: str = Field(default="", max_length=2000, description="该员工扮演的投顾老师人设")


class InspectionCreate(BaseModel):
    session_title: str | None = Field(default=None, max_length=200)
    raw_dialogue: str = Field(min_length=1)
    evaluatee: str | None = Field(default=None, max_length=50, description="本次评估对象（如 助理A）；为空时后端按唯一助理推导")


class ParsePreviewIn(BaseModel):
    raw_text: str = Field(min_length=1)


class BatchInspectionIn(BaseModel):
    """多人质检批次：完整聊天记录 + 助理规范名 → 员工 id 归属映射。"""

    raw_dialogue: str = Field(min_length=1)
    session_title: str | None = Field(default=None, max_length=200)
    mapping: dict[str, int]
    conversation_id: str | None = Field(default=None, max_length=64)


class ModelConfigIn(BaseModel):
    id: str | None = None
    name: str = ""
    protocol: str = "openai_compat"
    base_url: str = ""
    api_key: str = ""  # 编辑时留空 = 保留旧密钥
    model_name: str = ""
    temperature: float = 0.2


class TemplateIn(BaseModel):
    name: str = ""
    config: dict
