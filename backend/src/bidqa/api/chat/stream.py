"""POST /api/chat/stream（docs/01 §3.3/§3.4 B2）：Agent 问答 SSE 主端点（HTTP 适配层）。

职责只剩「接线」：限流前置 → 幂等守卫 → 可用性 5031 → 请求模型转 ChatJob →
起工作线程跑 application/chat/worker → SseChannel 泵事件。编排逻辑在
application/chat（不依赖 FastAPI），传输机制在 api/chat/sse，幂等在 api/chat/guards。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from bidqa.agent.assembly import build_done
from bidqa.api.chat.guards import check_client_msg_id
from bidqa.api.chat.schema import ChatStreamRequest
from bidqa.api.chat.sse import SseChannel
from bidqa.api.ratelimit import rate_identity
from bidqa.application.chat.availability import chat_available
from bidqa.application.chat.online import record_chat_done
from bidqa.auth.identity import ROLE_GUEST
from bidqa.config import get_settings
from bidqa.errors import EC_RATE_LIMITED, EC_SOURCE_DEGRADED, error_body

router = APIRouter()


@router.post("/api/chat/stream")
def chat_stream(payload: ChatStreamRequest, request: Request,
                settings: Any = Depends(get_settings)) -> Any:
    """SSE 主端点。settings 经 DI 注入（应用工厂 override 可测试）。"""
    from bidqa.obs.logs import note_request
    note_request("/api/chat/stream", 200)  # H2 行为计数（限流/4091 提前返回不计入）
    # 429 限流（B4；被限流请求不进入任何评测埋点；游客走独立更严配额桶）
    limiter, bucket = rate_identity(request, settings)
    allowed, retry_after = limiter.check(bucket)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content=error_body(EC_RATE_LIMITED, "请求过于频繁，请稍后再试",
                               detail={"retry_after": retry_after}),
            headers={"Retry-After": str(retry_after)},
        )
    # 4091 幂等（client_msg_id 60s 去重窗口；重试必须换新键）
    if (conflict := check_client_msg_id(payload.client_msg_id)) is not None:
        return JSONResponse(status_code=409,
                            content=error_body(4091, conflict["message"],
                                               detail=conflict["detail"]))

    # 前置可用性检查：该 provider 实际会用的聊天客户端缺失 → HTTP 5031
    # （口径与 worker 主路径一致：缺省/未知 provider 回主槽位；只配命名档不再误 5031）
    if not chat_available(payload.provider):
        return JSONResponse(
            status_code=503,
            content=error_body(EC_SOURCE_DEGRADED, "数据源降级/不可用",
                               detail={"source": "chat: 核心槽位未配置"}),
        )

    identity = getattr(request.state, "identity", None)
    from bidqa.application.chat.worker import ChatJob, ChatJobError, run_chat_job

    job = ChatJob(
        message=payload.message,
        conversation_id=payload.conversation_id,
        web_enabled=payload.web_enabled,
        thinking_enabled=payload.thinking_enabled,
        image_refs=payload.image_refs,
        provider=payload.provider,
        lang=payload.lang,
        thinking_model=settings.CHAT_THINKING_MODEL,
        is_guest=bool(identity and identity.role == ROLE_GUEST),
        user_id=(identity.user_id if identity else None),
    )
    channel = SseChannel()
    trace_id = uuid.uuid4().hex[:12]
    t0 = time.perf_counter()

    def deliver() -> None:
        """工作线程：编排 → _result；编排已内置降级，此层只兜 ChatJobError。"""
        try:
            outcome = run_chat_job(job, channel.emit)
        except ChatJobError as exc:
            channel.emit("_error", {"error_code": EC_SOURCE_DEGRADED,
                                    "message": str(exc)})
            return
        channel.emit("_result", outcome)
        if outcome.compress is not None:
            outcome.compress()  # C7 压缩：收尾事件已入队，5s 限时逻辑在 history 层

    threading.Thread(target=deliver, daemon=True, name="sse-worker").start()

    def build_done_event(outcome: Any, elapsed_ms: int) -> dict:
        done = build_done(outcome.result, elapsed_ms=elapsed_ms, trace_id=trace_id,
                          provider=payload.provider or "")
        record_chat_done(settings, question=payload.message,
                         provider=payload.provider, web_enabled=payload.web_enabled,
                         thinking_enabled=payload.thinking_enabled,
                         result=outcome.result, trace_id=trace_id,
                         elapsed_ms=elapsed_ms)
        return done

    return StreamingResponse(
        channel.lines(t0, build_done_event), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
