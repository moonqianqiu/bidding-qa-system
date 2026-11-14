"""POST /api/vision（docs/01 §3.1；图片解析 → image_ref）——HTTP 适配层。

业务用例在 application/vision（RefStore/describe_image，重构 C4 下沉）；本模块
只做限流/体积与 base64 校验/5031 映射。校验次序：传输层错误优先于能力校验
（413 → 400 → 5031）。聊天端点只收 image_refs（禁止 base64 直传聊天端点）。
"""

from __future__ import annotations

import base64
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bidqa.api.deps import degraded_503, rate_limited_429
from bidqa.api.ratelimit import rate_identity
from bidqa.application.vision import VisionUnavailable, describe_image, get_ref_store
from bidqa.config import get_settings
from bidqa.errors import EC_PAYLOAD_TOO_LARGE, error_body

router = APIRouter()

MAX_B64_CHARS = 4_000_000  # docs/01 §3.1：base64 超 4M 字符 → 413（参考项目同款口径）


class VisionRequest(BaseModel):
    image: str = Field(min_length=32, max_length=MAX_B64_CHARS + 64)
    filename: str | None = Field(default=None, max_length=200)


def _data_url(image: str) -> str:
    """data:image/* 直接用；裸 base64 视为 jpeg。解码失败抛 ValueError（→ 4001）。"""
    if image.startswith("data:image/") and ";base64," in image:
        payload = image.split(";base64,", 1)[1]
        data_url = image
    else:
        payload = image
        data_url = f"data:image/jpeg;base64,{image}"
    base64.b64decode(payload, validate=True)
    return data_url


@router.post("/api/vision")
def vision(payload: VisionRequest, request: Request,
           settings: Any = Depends(get_settings)) -> Any:
    from bidqa.errors import EC_VALIDATION
    from bidqa.obs.logs import note_request

    note_request("/api/vision", 200)  # H2 行为计数
    limiter, bucket = rate_identity(request, settings)  # 游客已被中间件 403 拦在前面
    allowed, retry_after = limiter.check(bucket)
    if not allowed:
        return rate_limited_429(retry_after)
    if len(payload.image) > MAX_B64_CHARS:  # 先挡体积再谈能力（传输层错误优先）
        return JSONResponse(
            status_code=413,
            content=error_body(EC_PAYLOAD_TOO_LARGE, "图片体积超限（base64 > 4M 字符）",
                               detail={"max_base64_chars": MAX_B64_CHARS}),
        )
    try:
        data_url = _data_url(payload.image)
    except Exception:
        return JSONResponse(
            status_code=400,
            content=error_body(EC_VALIDATION, "参数校验失败",
                               detail={"errors": [{"field": "image", "issue": "非法 base64"}]}),
        )
    try:
        description = describe_image(data_url)
    except VisionUnavailable as exc:
        return degraded_503(str(exc))
    ref = get_ref_store().put(description)
    return {"image_ref": ref, "description": description, "filename": payload.filename}
