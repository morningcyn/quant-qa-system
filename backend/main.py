# FastAPI 应用工厂：lifespan 建表 seed、静态前端挂载、统一错误处理
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api import api_router
from backend.config import FRONTEND_DIR
from backend.db.database import init_db
from backend.utils.errors import register_error_handlers


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="客服会话质检助手", lifespan=lifespan, docs_url=None, redoc_url=None)
    register_error_handlers(app)
    app.include_router(api_router)

    @app.get("/api/health")
    def health():
        return JSONResponse({"status": "ok"})

    # 前端静态页面挂载到根路径（需在 API 路由之后，避免遮蔽）
    if (FRONTEND_DIR / "index.html").exists():
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
    return app


app = create_app()
