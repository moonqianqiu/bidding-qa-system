"""POST /api/ask（docs/01 §3.1 B1；M1-8）：直连知识库问答（非流式，长期保留）——HTTP 适配层。

业务流程在 application/ask（检索+CRAG+生成，重构 C4 下沉）；本模块只做
限流/计数/5031 映射。长期保留语义：降级路径 + 评测 smoke 直连，不标 deprecated。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bidqa.api.ratelimit import rate_identity
from bidqa.application.ask import AskUnavailable, ask_direct
from bidqa.config import get_settings
from bidqa.errors import EC_RATE_LIMITED, EC_SOURCE_DEGRADED, error_body
from bidqa.rag.retrieve import FINAL_TOP_K

router = APIRouter()


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=FINAL_TOP_K, ge=1, le=20)


@router.post("/api/ask")
def ask(payload: AskRequest, request: Request,
        settings: Any = Depends(get_settings)) -> dict[str, Any]:
    """settings 经 DI 注入（工厂 override 可测试；C4 收口原直调 get_settings）。"""
    from bidqa.obs.logs import note_request

    limiter, bucket = rate_identity(request, settings)  # B4；游客走独立更严配额桶
    allowed, retry_after = limiter.check(bucket)
    if not allowed:  # B4：问答类端点限流；被限流不计在线评测
        return JSONResponse(
            status_code=429,
            content=error_body(
                EC_RATE_LIMITED,
                "请求过于频繁，请稍后再试",
                detail={"retry_after": retry_after},
            ),
            headers={"Retry-After": str(retry_after)},
        )
    note_request("/api/ask", 200)  # H2 行为计数
    try:
        return ask_direct(payload.question, payload.top_k)
    except AskUnavailable as exc:
        return JSONResponse(
            status_code=503,
            content=error_body(EC_SOURCE_DEGRADED, "数据源降级/不可用",
                               detail={"source": str(exc)}),
        )
