# SQLite 引擎与会话管理；init_db 建表 + seed 三套默认模板
import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.config import DATA_DIR, DB_PATH, DEFAULT_TEMPLATES
from backend.db.models import Base, ScoreTemplate

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False, "timeout": 30},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    _migrate()
    _seed_templates()


def _migrate() -> None:
    """兼容旧库：create_all 不会给已有表加列，这里按需 ALTER 补列（幂等）。"""
    with engine.connect() as conn:
        _ensure_column(conn, "assistants", "teacher_persona", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "inspections", "is_red_alert", "BOOLEAN NOT NULL DEFAULT 0")
        _ensure_column(conn, "inspections", "red_alert_reasons_json", "TEXT")
        conn.commit()


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})")
    cols = {row[1] for row in rows}
    if column not in cols:
        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _seed_templates() -> None:
    """score_templates 为空时写入三套默认模板（权重按打分机制图）。"""
    with SessionLocal() as session:
        existing = set(session.scalars(select(ScoreTemplate.template_type)))
        for ttype, config in DEFAULT_TEMPLATES.items():
            if ttype in existing:
                continue
            session.add(
                ScoreTemplate(
                    template_type=ttype,
                    name=config["name"],
                    config_json=json.dumps(config, ensure_ascii=False),
                )
            )
        session.commit()


def get_session():
    return SessionLocal()


def get_db():
    """FastAPI 依赖：请求级会话。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
