"""统一响应与错误模型（docs/01 §3.2）：所有非 2xx 响应使用同一错误体。

错误体：{"error_code": int, "message": str, "trace_id": str, "detail": object|null}
M1（B6）起契约测试覆盖每个错误码至少一例；M0 先落 4041/4011/5001/4001 四条路径。
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

EC_VALIDATION = 4001  # 参数校验失败（detail 含字段级错误）
EC_UNAUTHORIZED = 4011  # Bearer token 缺失/无效
EC_FORBIDDEN = 4031  # 角色不足/能力被禁（游客访问受限路径、游客开关已关闭）
EC_NOT_FOUND = 4041  # 资源不存在（非法标识一律走这里）
EC_CONFLICT = 4091  # 重复提交（client_msg_id 去重窗口）/ 资源冲突（用户名已存在）
EC_PAYLOAD_TOO_LARGE = 4131  # 上传体积超限
EC_RATE_LIMITED = 4291  # 触发限流（响应带 Retry-After）
EC_INTERNAL = 5001  # 未分类服务端错误
EC_SOURCE_DEGRADED = 5031  # 数据源降级/不可用
EC_JUDGE_FINGERPRINT = 5032  # 判分模型异源校验失败

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


def error_body(code: int, message: str, detail: object | None = None) -> dict:
    return {
        "error_code": code,
        "message": message,
        "trace_id": uuid.uuid4().hex[:12],
        "detail": detail,
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
