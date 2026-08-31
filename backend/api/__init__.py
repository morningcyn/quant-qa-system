# 路由汇总：统一挂 /api 前缀
from fastapi import APIRouter

from backend.api import assistants, batch, emotion, inspections, overviews, parse, reports, settings, stats

api_router = APIRouter(prefix="/api")
api_router.include_router(assistants.router)
api_router.include_router(parse.router)
api_router.include_router(inspections.router)
api_router.include_router(overviews.router)
api_router.include_router(reports.router)
api_router.include_router(stats.router)
api_router.include_router(settings.router)
api_router.include_router(batch.router)
api_router.include_router(emotion.router)
