"""全局异常处理安装（docs/01 §3.2）：所有非 2xx 走统一错误体。

从 errors.py 拆出（v2.2）：错误码表与 error_body 是各层共用的纯契约；
FastAPI 处理器是 HTTP 适配层的职责——业务层 import errors 时不再传递性
依赖 FastAPI（2026-11-14 分层审计：clients→errors→fastapi 传递边）。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
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

    @app.exception_handler(Exception)
    async def on_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content=error_body(EC_INTERNAL, "未分类服务端错误"),
        )
