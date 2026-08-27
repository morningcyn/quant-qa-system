# SQLAlchemy ORM：assistants / inspections / inspection_details / score_templates / settings
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Assistant(Base):
    __tablename__ = "assistants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    employee_no: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    template_type: Mapped[str] = mapped_column(String(20), nullable=False, default="standard")
    # 该员工当前扮演的"高级投顾老师"人设（注入质检提示词，影响打分与黄金改写的语气）
    teacher_persona: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    inspections: Mapped[list["Inspection"]] = relationship(
        back_populates="assistant", cascade="all, delete-orphan"
    )


class Inspection(Base):
    __tablename__ = "inspections"
    __table_args__ = (Index("idx_inspections_ast_time", "assistant_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    assistant_id: Mapped[int] = mapped_column(
        ForeignKey("assistants.id", ondelete="CASCADE"), nullable=False
    )
    session_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    total_score: Mapped[int] = mapped_column(Integer, nullable=False)
    is_yellow_alert: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    yellow_alert_reasons_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 红灯一票否决：合规违规（承诺收益/代客理财/报具体点位）专用，独立于黄灯
    is_red_alert: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    red_alert_reasons_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_type: Mapped[str] = mapped_column(String(20), nullable=False, default="standard")
    # 打分时使用的模板完整快照：模板可配置化后，历史雷达图满分与 Top3 失分计算必须用当时的模板
    template_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    customer_profile: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 本次评估对象（如 助理A）；多角色对话只对该对象计分，其余角色为上下文背景
    evaluatee: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # N/A 豁免维度 [{key, name, reason, max}] 与动态分母（满分 100 − Σ豁免维度满分）
    na_dims_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 多人质检：本次报告所属的客户服务会话 ID（dispatcher 事后补写，单助理路径为 None）
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    assistant: Mapped["Assistant"] = relationship(back_populates="inspections")
    detail: Mapped["InspectionDetail | None"] = relationship(
        back_populates="inspection", cascade="all, delete-orphan", uselist=False
    )


class InspectionDetail(Base):
    __tablename__ = "inspection_details"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    inspection_id: Mapped[int] = mapped_column(
        ForeignKey("inspections.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    raw_dialogue: Mapped[str] = mapped_column(Text, nullable=False)
    d_scores_json: Mapped[str] = mapped_column(Text, nullable=False)
    s_scores_json: Mapped[str] = mapped_column(Text, nullable=False)
    highlight_dialogue_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggestions_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    inspection: Mapped["Inspection"] = relationship(back_populates="detail")


class ServiceOverview(Base):
    """多人质检总览：一次客户服务的完整会话 → 各助理质检报告 + LLM/规则总览。"""

    __tablename__ = "service_overviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 完整原始聊天记录（需求：保留完整原始对话，可追溯）
    raw_dialogue: Mapped[str] = mapped_column(Text, nullable=False)
    # {summary: {main_strengths, main_issues, customer_issue_resolved, resolution_reason, overall_comment},
    #  participants: [...], degraded: bool}
    summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 关联报告 inspection id 列表
    inspection_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class ScoreTemplate(Base):
    __tablename__ = "score_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    template_type: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )


class Setting(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )
