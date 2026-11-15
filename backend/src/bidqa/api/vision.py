"""POST /api/vision（docs/01 §3.1；图片解析 → image_ref）——HTTP 适配层。

业务用例在 application/image（refs/analyze，v2.3 双通道路由）；本模块只做
限流/体积与 base64 校验/mode 合法性/5031 映射。校验次序：传输层错误优先于
能力校验（413 → 400 → 5031）。聊天端点只收 image_refs（禁止 base64 直传聊天端点）。
"""

from __future__ import annotations

import base64
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bidqa.api.deps import degraded_503, rate_limited_429
from bidqa.api.ratelimit import rate_identity
from bidqa.application.image.analyze import analyze_image
from bidqa.application.image.qwen import VisionUnavailable
from bidqa.application.image.refs import get_ref_store
from bidqa.config import get_settings
from bidqa.errors import EC_PAYLOAD_TOO_LARGE, error_body

router = APIRouter()

MAX_B64_CHARS = 4_000_000  # docs/01 §3.1：base64 超 4M 字符 → 413（参考项目同款口径，逐字段）
MODES = ("auto", "ocr", "describe")


class VisionRequest(BaseModel):
    image: str = Field(min_length=32, max_length=MAX_B64_CHARS + 64)  # 压缩预览（Qwen 路由用）
    original: str | None = Field(default=None, max_length=MAX_B64_CHARS + 64)  # 高分辨率原件（OCR 用）
    mode: str = "auto"  # auto=Qwen 路由 | ocr=手动按文档解析 | describe=强制通用描述
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


def _bad(errors: list[dict[str, str]]) -> JSONResponse:
    from bidqa.errors import EC_VALIDATION

    return JSONResponse(status_code=400,
                        content=error_body(EC_VALIDATION, "参数校验失败", detail={"errors": errors}))


@router.post("/api/vision")
def vision(payload: VisionRequest, request: Request,
           settings: Any = Depends(get_settings)) -> Any:
    from bidqa.obs.logs import note_request

    def _note(status: int) -> None:
        note_request("/api/vision", status)  # P2-5：按真实状态码计数（原写死 200 在判定前）

    limiter, bucket = rate_identity(request, settings)  # 游客已被中间件 403 拦在前面
    allowed, retry_after = limiter.check(bucket)
    if not allowed:
        _note(429)
        return rate_limited_429(retry_after)
    fields = (("image", payload.image), ("original", payload.original))
    oversize = [name for name, v in fields if v and len(v) > MAX_B64_CHARS]
    if oversize:  # 先挡体积再谈能力（传输层错误优先）
        _note(413)
        return JSONResponse(
            status_code=413,
            content=error_body(EC_PAYLOAD_TOO_LARGE, "图片体积超限（base64 > 4M 字符）",
                               detail={"max_base64_chars": MAX_B64_CHARS, "fields": oversize}),
        )
    try:
        data_url = _data_url(payload.image)
        original_url = _data_url(payload.original) if payload.original else None
    except Exception:
        _note(400)
        return _bad([{"field": "image", "issue": "非法 base64"}])
    if payload.mode not in MODES:
        _note(400)
        return _bad([{"field": "mode", "issue": "须为 auto/ocr/describe"}])
    if payload.mode == "ocr":
        if not settings.has_ocr:
            _note(503)
            return degraded_503("ocr: OCR 槽位未配置（OCR_*）")
        if not payload.original:
            _note(400)
            return _bad([{"field": "original", "issue": "mode=ocr 需要高分辨率原件"}])
    try:
        result = analyze_image(data_url, original_url, payload.mode)
    except VisionUnavailable as exc:
        _note(503)
        return degraded_503(str(exc))
    _note(200)
    ref = get_ref_store().put(result.text)
    return {"image_ref": ref, "description": result.text, "filename": payload.filename,
            "mode_used": result.mode_used, "route_by": result.route_by,
            "degraded": result.degraded}
