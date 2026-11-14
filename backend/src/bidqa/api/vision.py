"""POST /api/vision（docs/01 §3.1；图片解析 → image_ref）。

流程：体积校验（base64 > 4M 字符 → 413/4131，参考项目同款口径）→ 视觉槽位解析
（多模态 list-content，同一 OpenAI-compatible 适配器）→ 描述入进程内 TTL 缓存
（10 分钟，§3.3「服务端缓存图片描述」；单进程约定 docs/01 §6）→
返回 {image_ref, description, filename}。聊天端点只收 image_refs（禁止 base64
直传聊天端点），worker 按 ref 取缓存描述注入上下文。仅解析不检索；
解析失败 5031，绝不返回空描述充数。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

import base64

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bidqa.api.ratelimit import rate_identity
from bidqa.config import get_settings
from bidqa.errors import (
    EC_PAYLOAD_TOO_LARGE, EC_RATE_LIMITED, EC_SOURCE_DEGRADED, EC_VALIDATION, error_body)
from bidqa.service import build_vision

router = APIRouter()

MAX_B64_CHARS = 4_000_000  # docs/01 §3.1：base64 超 4M 字符 → 413（参考项目同款口径）
REF_TTL_SECONDS = 600.0  # §3.3：服务端缓存图片描述 TTL 10 分钟
REF_STORE_MAX = 64  # 有界防泄漏；满了先清过期再丢最旧（web_cache 同款策略）

VISION_SYSTEM_PROMPT = (
    "你是招投标文件图片解析助手。只描述图片中真实可见的内容（文字/表格/数字/布局），"
    "不推测、不编造；看不清的部分如实说明。")
VISION_USER_PROMPT = "请解析这张图片的内容，输出结构化的文字描述。"


class VisionRequest(BaseModel):
    image: str = Field(min_length=32, max_length=MAX_B64_CHARS + 64)
    filename: str | None = Field(default=None, max_length=200)


class RefStore:
    """image_ref → 描述的进程内 TTL 缓存（单进程约定；重启即清，前端重传即可）。"""

    def __init__(self, ttl: float = REF_TTL_SECONDS, max_entries: int = REF_STORE_MAX):
        self.ttl = ttl
        self.max_entries = max_entries
        self._items: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def put(self, description: str) -> str:
        now = time.monotonic()
        with self._lock:
            if len(self._items) >= self.max_entries:
                alive = {k: v for k, v in self._items.items() if now - v[1] < self.ttl}
                if len(alive) >= self.max_entries:  # 全活着：丢最旧（web_cache 同款策略）
                    oldest = sorted(alive, key=lambda k: alive[k][1])
                    for k in oldest[:len(alive) - self.max_entries + 1]:
                        alive.pop(k, None)
                self._items = alive
            ref = f"img_{uuid.uuid4().hex[:12]}"
            self._items[ref] = (description, now)
            return ref

    def get(self, ref: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            item = self._items.get(ref)
            if item is None:
                return None
            description, ts = item
            if now - ts >= self.ttl:
                self._items.pop(ref, None)
                return None
            return description


_store = RefStore()


def get_ref_store() -> RefStore:
    return _store


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
def vision(payload: VisionRequest, request: Request) -> Any:
    settings = get_settings()
    from bidqa.api.system import note_request
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
    ref = _store.put(description)
    return {"image_ref": ref, "description": description, "filename": payload.filename}
