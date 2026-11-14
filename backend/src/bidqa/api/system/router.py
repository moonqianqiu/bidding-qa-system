"""/api/system 路由聚合（重构 C4 拆分后只剩接线）。

端点按职责分模块：metrics（观测 HTTP 视图）/ console（三库聚合+缓存）/
providers（档位名册）/ users（用户管理+游客开关）/ conv_admin（全库会话旁路）。
admin-only 门禁由中间件按 /api/system 前缀统一执行（providers 除外）。
"""

from __future__ import annotations

from fastapi import APIRouter

from bidqa.api.system.console import router as console_router
from bidqa.api.system.conv_admin import router as conv_admin_router
from bidqa.api.system.metrics import router as metrics_router
from bidqa.api.system.providers import router as providers_router
from bidqa.api.system.users import router as users_router

router = APIRouter()
router.include_router(metrics_router)
router.include_router(console_router)
router.include_router(providers_router)
router.include_router(users_router)
router.include_router(conv_admin_router)
