"""API 公共响应 helper（docs/01 §3.2）：503/429 错误体单处拼装。

v2.2 收口：原先 ask/auth/conversations/system/chat/vision 各自重写「5031 数据源
降级」与「429 限流」响应块（7 处 503、3 处 429，文案/结构已开始漂移）。这里只
保留两种 HTTP 形态，业务模块传 source / retry_after——错误体逐字与旧实现一致。
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from bidqa.errors import EC_RATE_LIMITED, EC_SOURCE_DEGRADED, error_body


def degraded_503(source: str) -> JSONResponse:
    """数据源降级/不可用（5031）：detail.source 注明哪个能力缺失。"""
    return JSONResponse(
        status_code=503,
        content=error_body(EC_SOURCE_DEGRADED, "数据源降级/不可用",
                           detail={"source": source}),
    )


def rate_limited_429(retry_after: int) -> JSONResponse:
    """限流（4291）：带 Retry-After 头（客户端可据此退避）。"""
    return JSONResponse(
        status_code=429,
        content=error_body(EC_RATE_LIMITED, "请求过于频繁，请稍后再试",
                           detail={"retry_after": retry_after}),
        headers={"Retry-After": str(retry_after)},
    )
