"""POST /api/vision（docs/01 §3.1；图片解析 → image_ref）——HTTP 适配层。

流程：体积校验（base64 > 4M 字符 → 413/4131，参考项目同款口径）→ 视觉槽位解析
（多模态 list-content，同一 OpenAI-compatible 适配器）→ 描述入 application 层
TTL 缓存（10 分钟，§3.3；chat worker 经 application.vision 读取）→
返回 {image_ref, description, filename}。聊天端点只收 image_refs（禁止 base64
直传聊天端点）。仅解析不检索；解析失败 5031，绝不返回空描述充数。
ref 状态与 image_context_block 组装在 application/vision（重构 C3 归位）。
"""

from __future__ import annotations

import base64

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Any

from bidqa.api.ratelimit import rate_identity
from bidqa.application.vision import RefStore, get_ref_store  # noqa: F401  # 再导出：测试与 chat 共用
from bidqa.config import get_settings
from bidqa.errors import (
    EC_PAYLOAD_TOO_LARGE, EC_RATE_LIMITED, EC_SOURCE_DEGRADED, EC_VALIDATION, error_body)
from bidqa.service import build_vision

router = APIRouter()

MAX_B64_CHARS = 4_000_000  # docs/01 §3.1：base64 超 4M 字符 → 413（参考项目同款口径）

VISION_SYSTEM_PROMPT = (
    "你是招投标文件图片解析助手。只描述图片中真实可见的内容（文字/表格/数字/布局），"
    "不推测、不编造；看不清的部分如实说明。")
VISION_USER_PROMPT = "请解析这张图片的内容，输出结构化的文字描述。"


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
    # settings 经 DI 注入（main.py 工厂 override 可测试）——此前直调 get_settings()
    # 绕过注入，测试要靠真实 .env 碰巧配置了 VISION 才能过（2026-11-14 新仓复现修复）
    from bidqa.obs.logs import note_request
    note_request("/api/vision", 200)  # H2 行为计数
    limiter, bucket = rate_identity(request, settings)  # 游客已被中间件 403 拦在前面
    allowed, retry_after = limiter.check(bucket)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content=error_body(EC_RATE_LIMITED, "请求过于频繁，请稍后再试",
                               detail={"retry_after": retry_after}),
            headers={"Retry-After": str(retry_after)},
        )
    if len(payload.image) > MAX_B64_CHARS:  # 先挡体积再谈能力（传输层错误优先）
        return JSONResponse(
            status_code=413,
            content=error_body(EC_PAYLOAD_TOO_LARGE, "图片体积超限（base64 > 4M 字符）",
                               detail={"max_base64_chars": MAX_B64_CHARS}),
        )
    if not settings.has_vision:
        return JSONResponse(
            status_code=503,
            content=error_body(EC_SOURCE_DEGRADED, "数据源降级/不可用",
                               detail={"source": "vision: 视觉槽位未配置（VISION_*）"}),
        )
    try:
        data_url = _data_url(payload.image)
    except Exception:
        return JSONResponse(
            status_code=400,
            content=error_body(EC_VALIDATION, "参数校验失败",
                               detail={"errors": [{"field": "image", "issue": "非法 base64"}]}),
        )
    vision_client = build_vision()
    if vision_client is None:
        return JSONResponse(
            status_code=503,
            content=error_body(EC_SOURCE_DEGRADED, "数据源降级/不可用",
                               detail={"source": "vision: 视觉槽位未配置（VISION_*）"}),
        )
    try:
        description = str(vision_client.chat([
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": VISION_USER_PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ], temperature=0.1)).strip()
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content=error_body(EC_SOURCE_DEGRADED, "数据源降级/不可用",
                               detail={"source": f"vision: {type(exc).__name__}: {str(exc)[:120]}"}),
        )
    if not description:
        return JSONResponse(
            status_code=503,
            content=error_body(EC_SOURCE_DEGRADED, "数据源降级/不可用",
                               detail={"source": "vision: 上游返回空描述"}),
        )
    ref = get_ref_store().put(description)
    return {"image_ref": ref, "description": description, "filename": payload.filename}
