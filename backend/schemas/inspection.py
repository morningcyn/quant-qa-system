# LLM 输出 JSON 契约（Pydantic v2）—— 质检正确性的基石。
# v2（2026-08-25 重构）：
#  - 红灯一票否决：is_red_alert / red_alert_reasons，专用于合规违规（承诺收益、代客理财、报具体点位）；
#    黄灯仅表示业务瑕疵（评分偏低）。
#  - 强制思维链：每个底层打分项在 score 前带 analysis（先盘点事实再给分）。
#  - 算术剥离：模型只打底层分（D 各维度 score / S 各子项 score）；total_score 与 S 维度 score 可省略
#    （default=0），由后端 scoring.apply_guardrails 汇总覆盖，不信任模型算术。
from pydantic import BaseModel, ConfigDict, Field, model_validator

# 单次质检最多允许 N/A 的维度数（防模型偷懒全标 N/A 拿满分）
MAX_NA_DIMS = 2


def _check_na(score, na_reason):
    """score=null（N/A）时必须给出无法判定的原因，否则视为非法输出触发重试。"""
    if score is None and not (na_reason or "").strip():
        raise ValueError("score 为 null（N/A）时必须提供 na_reason 说明无法判定的原因")


class SubItem(BaseModel):
    """S 端子项（底层打分项）：先 analysis 盘点事实，再给 score。"""

    analysis: str = Field(default="", description="打分依据：先盘点该子项对应的事实，再给分")
    score: int | None = Field(ge=0, description="子项实得分；所属维度 N/A 时输出 null")

    @model_validator(mode="before")
    @classmethod
    def _from_legacy_int(cls, v):
        """兼容旧格式输出（子项直接给整数）→ 自动补空 analysis。"""
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return {"analysis": "", "score": int(v)}
        return v


class D1Score(BaseModel):
    """D1 情绪转化（10分）：客户情绪开场→收尾的变化。"""

    analysis: str = Field(default="", description="打分依据：盘点情绪变化事实后再给分")
    score: int | None = Field(ge=0, description="实得分；无法判定时输出 null（N/A）")
    na_reason: str = Field(default="", description="score 为 null 时的无法判定原因（如 客户未表达情绪）")
    rating: str = Field(default="", description="情绪变化评级")
    comment: str = Field(default="", description="评语")

    @model_validator(mode="after")
    def _na(self):
        _check_na(self.score, self.na_reason)
        return self


class D2Score(BaseModel):
    """D2 画像匹配（15分）：客户画像识别与匹配。"""

    analysis: str = Field(default="", description="打分依据：盘点画像识别与安抚事实后再给分")
    profile: str = Field(default="", description="客户画像，如 焦虑型")
    score: int | None = Field(ge=0, description="实得分；无法判定时输出 null（N/A）")
    na_reason: str = Field(default="", description="score 为 null 时的无法判定原因（如 客户未表达情绪）")
    match_rating: str = Field(default="", description="匹配程度评级")
    comment: str = Field(default="")

    @model_validator(mode="after")
    def _na(self):
        _check_na(self.score, self.na_reason)
        return self


class D3Score(BaseModel):
    """D3 诉求穿透（15分）：表面诉求 vs 深层诉求。"""

    analysis: str = Field(default="", description="打分依据：盘点诉求识别与方案事实后再给分")
    score: int | None = Field(ge=0, description="实得分；无法判定时输出 null（N/A）")
    na_reason: str = Field(default="", description="score 为 null 时的无法判定原因")
    surface_vs_deep: str = Field(default="", description="看懂底层/看懂部分/只看表面/没看懂")
    resolution: str = Field(default="", description="方案与深层诉求的匹配度")
    comment: str = Field(default="")

    @model_validator(mode="after")
    def _na(self):
        _check_na(self.score, self.na_reason)
        return self


class D4Score(BaseModel):
    """D4 预期超越（15分）：衍生问题预判 + 掌控感交还。"""

    analysis: str = Field(default="", description="打分依据：盘点预判与掌控感交还事实后再给分")
    score: int | None = Field(ge=0, description="实得分；无法判定时输出 null（N/A）")
    na_reason: str = Field(default="", description="score 为 null 时的无法判定原因")
    derived_question: int = Field(default=0, ge=0, description="主动预判的衍生问题数量")
    control_given: int = Field(default=0, ge=0, description="交还掌控感的动作数量")
    comment: str = Field(default="")

    @model_validator(mode="after")
    def _na(self):
        _check_na(self.score, self.na_reason)
        return self


class DScores(BaseModel):
    d1_emotion_change: D1Score
    d2_profile_match: D2Score
    d3_problem_match: D3Score
    d4_expectation_exceed: D4Score


class S1SubItems(BaseModel):
    empathy: SubItem  # 共情 4
    customized: SubItem  # 定制化 5
    direct: SubItem  # 直接 4
    no_conflict: SubItem  # 无冲突 5
    vent_guide: SubItem  # 宣泄引导 2


class S2SubItems(BaseModel):
    completeness: SubItem  # 完整性 4
    structure: SubItem  # 结构化 4
    next_step: SubItem  # 下一步动作 3
    follow_up: SubItem  # 跟进承诺 4


class S3SubItems(BaseModel):
    logic: SubItem  # 逻辑 4
    explain_why: SubItem  # 讲原因 3
    decision_ownership: SubItem  # 决策归属 3


class S1Score(BaseModel):
    """S1 情绪维度（20分）。score 为汇总值：算术剥离后由后端按子项求和得出，模型可省略；
    无法判定时输出 null（N/A）+ na_reason，子项一并豁免。"""

    score: int | None = Field(default=0, ge=0)
    na_reason: str = Field(default="", description="score 为 null 时的无法判定原因")
    sub_items: S1SubItems

    @model_validator(mode="after")
    def _na(self):
        _check_na(self.score, self.na_reason)
        return self


class S2Score(BaseModel):
    """S2 问题闭环（15分）。score 由后端按子项求和得出，模型可省略；
    无法判定时输出 null（N/A）+ na_reason，子项一并豁免。"""

    score: int | None = Field(default=0, ge=0)
    na_reason: str = Field(default="", description="score 为 null 时的无法判定原因")
    sub_items: S2SubItems

    @model_validator(mode="after")
    def _na(self):
        _check_na(self.score, self.na_reason)
        return self


class S3Score(BaseModel):
    """S3 专业供给（10分）。score 由后端按子项求和得出，模型可省略；
    无法判定时输出 null（N/A）+ na_reason，子项一并豁免。"""

    score: int | None = Field(default=0, ge=0)
    na_reason: str = Field(default="", description="score 为 null 时的无法判定原因")
    sub_items: S3SubItems

    @model_validator(mode="after")
    def _na(self):
        _check_na(self.score, self.na_reason)
        return self


class SScores(BaseModel):
    s1_emotion_stabilize: S1Score
    s2_problem_closure: S2Score
    s3_professional_supply: S3Score


class HighlightItem(BaseModel):
    """扣分点与 AI 黄金改写对比。"""

    turn: int = Field(ge=1, description="对话轮次号（必须与输入编号对齐）")
    role: str = Field(default="", description="该轮角色：客/助")
    original_text: str = Field(default="", description="原话")
    issue_type: str = Field(default="", description="扣分维度编号，如 S1-1, S2-3")
    ai_rewrite: str = Field(default="", description="AI 高分改写版（必须符合三道红线与老师人设）")


class LLMResultSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # 算术剥离：总分由后端按底层分重算，模型可省略（default=0）
    total_score: int = Field(default=0, ge=0, le=100)
    # 红灯一票否决：合规违规（承诺收益/代客理财/报具体点位）专用，与总分无关
    is_red_alert: bool = Field(default=False)
    red_alert_reasons: list[str] = Field(default_factory=list)
    # 黄灯仅表示业务瑕疵（评分偏低），由后端按总分强制重算
    is_yellow_alert: bool = Field(default=False)
    yellow_alert_reasons: list[str] = Field(default_factory=list)
    d_scores: DScores
    s_scores: SScores
    highlight_dialogue: list[HighlightItem] = Field(default_factory=list)
    improvement_suggestions: list[str] = Field(default_factory=list)
    # 系统字段（guardrails 输出，模型无需输出）：N/A 豁免维度与动态分母
    effective_max: int | None = Field(default=None, description="系统字段：扣除 N/A 维度后的有效满分")
    na_dims: list[dict] = Field(default_factory=list, description="系统字段：N/A 豁免维度 [{key,name,reason,max}]")

    @model_validator(mode="after")
    def _na_limit(self):
        """N/A 豁免上限：单次最多 MAX_NA_DIMS 个维度，超限视为非法输出触发重试（防模型偷懒全标 N/A）。"""
        na_dims = [
            d for d in (
                self.d_scores.d1_emotion_change,
                self.d_scores.d2_profile_match,
                self.d_scores.d3_problem_match,
                self.d_scores.d4_expectation_exceed,
                self.s_scores.s1_emotion_stabilize,
                self.s_scores.s2_problem_closure,
                self.s_scores.s3_professional_supply,
            )
            if d.score is None
        ]
        if len(na_dims) > MAX_NA_DIMS:
            raise ValueError(
                f"N/A（无法判定）维度最多 {MAX_NA_DIMS} 个，当前 {len(na_dims)} 个，"
                "请为其余维度依据文本可见信息给出实际分数"
            )
        return self
