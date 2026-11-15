"""FastAPI 应用工厂（docs/01 §1/§3）：CORS 白名单 + Bearer 鉴权 + 统一错误体。

端点同步 def（线程池执行，慢请求不阻塞事件循环）。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bidqa.api import health
from bidqa.api.error_handlers import install_error_handlers
from bidqa.api.middleware import BearerAuthMiddleware
from bidqa.config import Settings, get_settings

APP_VERSION = "0.1.0"


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        # 生产默认路径门禁（审计 P1-6）：`uvicorn bidqa.main:app` 直拉同样过
        # 核心校验与 workers=1 防呆，不得绕过 `bidqa serve`。
        # 显式传 Settings 的调用方（测试/嵌入式）自带配置责任，不强校验——
        # health 能力降级用例需要构造「核心缺失」的应用形态。
        settings = get_settings()
        settings.validate_core()
    app = FastAPI(title="bidqa", version=APP_VERSION)
    # 端点统一经 Depends(get_settings) 取配置；应用工厂注入时覆盖之，
    # 保证 create_app(自定义Settings) 的实例在所有端点生效（可测试性）。
    app.dependency_overrides[get_settings] = lambda: settings

    origins = settings.cors_origins()

    # 注意注册顺序：Starlette 后添加者先执行（=最外层）。鉴权先加（内层）、
    # CORS 后加（最外层）——预检 OPTIONS 不带 Authorization，若被鉴权中间件
    # 先拦会 401，浏览器侧全部跨域请求直接失败（2026-11-09 实测修复）。
    app.add_middleware(
        BearerAuthMiddleware,
        token=settings.API_AUTH_TOKEN,
        users_enabled=settings.USERS_ENABLED,
    )
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,  # Bearer 头鉴权，不使用 Cookie
            allow_methods=["*"],
            allow_headers=["*"],
        )
    install_error_handlers(app)
    app.include_router(health.router)
    from bidqa.api.ask import router as ask_router
    from bidqa.api.auth import router as auth_router
    from bidqa.api.chat.stream import router as chat_stream_router
    from bidqa.api.conversations import router as conversations_router
    from bidqa.api.eval_runs import router as eval_router
    from bidqa.api.system.router import router as system_router
    from bidqa.api.vision import router as vision_router

    app.include_router(auth_router)
    app.include_router(ask_router)
    app.include_router(chat_stream_router)
    app.include_router(conversations_router)
    app.include_router(system_router)
    app.include_router(eval_router)
    app.include_router(vision_router)
    from bidqa.obs.logs import setup_log_buffer

    # 日志缓冲随服务常开（H2）；评测门只影响 /api/eval 可见性
    setup_log_buffer()
    return app


app = create_app()
