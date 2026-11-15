"""全局异常处理安装（docs/01 §3.2）：所有非 2xx 走统一错误体。

从 errors.py 拆出（v2.2）：错误码表与 error_body 是各层共用的纯契约；
FastAPI 处理器是 HTTP 适配层的职责——业务层 import errors 时不再传递性
依赖 FastAPI（2026-11-14 分层审计：clients→errors→fastapi 传递边）。
"""

from __future__ import annotations

import logging

import psycopg
from fastapi import FastAPI, Request

logger = logging.getLogger("bidqa.api.error_handlers")
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from bidqa.errors import (
    EC_CONFLICT,
    EC_FORBIDDEN,
    EC_INTERNAL,
    EC_NOT_FOUND,
    EC_PAYLOAD_TOO_LARGE,
    EC_RATE_LIMITED,
    EC_SOURCE_DEGRADED,
    EC_UNAUTHORIZED,
    EC_VALIDATION,
    error_body,
)

_HTTP_STATUS_TO_CODE = {
    400: EC_VALIDATION,
    401: EC_UNAUTHORIZED,
    403: EC_FORBIDDEN,
    404: EC_NOT_FOUND,
    409: EC_CONFLICT,
    413: EC_PAYLOAD_TOO_LARGE,
    429: EC_RATE_LIMITED,
    500: EC_INTERNAL,
    503: EC_SOURCE_DEGRADED,
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=error_body(EC_VALIDATION, "参数校验失败", detail={"errors": exc.errors()}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def on_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_STATUS_TO_CODE.get(exc.status_code, EC_INTERNAL)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, str(exc.detail)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(psycopg.Error)
    async def on_pg_error(request: Request, exc: psycopg.Error) -> JSONResponse:
        # PG 中途断连/不可用（resolve/login 等存储路径无 try/except 的兜底，审计 P1-2）：
        # 契约要求 503/5031（数据源降级），此前落全局 500/5001
        return JSONResponse(
            status_code=503,
            content=error_body(EC_SOURCE_DEGRADED, "postgres: 不可用"),
        )

    @app.exception_handler(Exception)
    async def on_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未分类服务端错误 path=%s", request.url.path)  # 带堆栈，可对账
        return JSONResponse(
            status_code=500,
            content=error_body(EC_INTERNAL, "未分类服务端错误"),
        )
