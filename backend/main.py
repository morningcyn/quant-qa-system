# FastAPI 应用工厂：lifespan 建表 seed、静态前端挂载、统一错误处理
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from backend.api import api_router
from backend.config import FRONTEND_DIR
from backend.db import batch_repository as brepo
from backend.db.database import SessionLocal, init_db
from backend.services.batch.manager import mgr
from backend.utils.errors import register_error_handlers


class NoCacheStaticFiles(StaticFiles):
    """前端静态资源总是 revalidate（Cache-Control: no-cache）：

    文件变更后浏览器刷新即拿到新版本，避免旧 JS/CSS 缓存导致页面行为滞后
    （如主题切换按钮看不到、切换无效）。未变更时仍走 304，无重复下载开销。
    """

    def file_response(self, *args, **kwargs) -> Response:
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 断点续跑：处理中超时（进程强杀遗留）→ 置回 pending；pending 自动续跑、
    # completed 不重跑、failed 不自动重跑（等用户修复后手动点「重新评分失败任务」）
    with SessionLocal() as session:
        brepo.reset_stale_processing(session, older_than=timedelta(minutes=10))
    mgr.resume_all()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="助理会话质检助手", lifespan=lifespan, docs_url=None, redoc_url=None)
    register_error_handlers(app)
    app.include_router(api_router)

    @app.get("/api/health")
    def health():
        return JSONResponse({"status": "ok"})

    # 前端静态页面挂载到根路径（需在 API 路由之后，避免遮蔽）
    if (FRONTEND_DIR / "index.html").exists():
        app.mount("/", NoCacheStaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
    return app


app = create_app()
