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

from bidqa.api.chat.guards import check_client_msg_id
from bidqa.api.chat.schema import ChatStreamRequest
from bidqa.api.chat.sse import SseChannel
from bidqa.api.conversations import _scope
from bidqa.api.deps import degraded_503, rate_limited_429
from bidqa.api.ratelimit import rate_identity
from bidqa.application.chat.availability import chat_available
from bidqa.application.chat.done import build_done_event as compose_done_event
from bidqa.application.chat.online import record_chat_done
from bidqa.auth.identity import ROLE_GUEST
from bidqa.config import get_settings
from bidqa.database.conversations import is_uuid
from bidqa.errors import EC_NOT_FOUND, EC_SOURCE_DEGRADED, error_body

router = APIRouter()


@router.post("/api/chat/stream")
def chat_stream(payload: ChatStreamRequest, request: Request,
                settings: Any = Depends(get_settings)) -> Any:
    """SSE 主端点。settings 经 DI 注入（应用工厂 override 可测试）。"""
    from bidqa.obs.logs import note_request

    def _note(status: int) -> None:
        note_request("/api/chat/stream", status)  # P2-5：按真实状态码计数（原写死 200 且在判定前调用）

    identity = getattr(request.state, "identity", None)
    # 429 限流（B4；被限流请求不进入任何评测埋点；游客走独立更严配额桶）
    limiter, bucket = rate_identity(request, settings)
    allowed, retry_after = limiter.check(bucket)
    if not allowed:
        _note(429)
        return rate_limited_429(retry_after)
    # 4091 幂等（client_msg_id 60s 去重窗口；重试必须换新键；owner 分域防跨用户误撞）
    owner = (identity.token or identity.user_id or "anon") if identity else "anon"
    if (conflict := check_client_msg_id(payload.client_msg_id, owner=owner)) is not None:
        _note(409)
        return JSONResponse(status_code=409,
                            content=error_body(4091, conflict["message"],
                                               detail=conflict["detail"]))

    # P1-27（审计）：游客锁默认档——provider 请求参数对游客一律忽略（localStorage
    # 偏好曾绕过 UI 锁直传更贵命名档）。服务端是门禁真源；可用性检查也按有效档。
    is_guest = bool(identity and identity.role == ROLE_GUEST)
    effective_provider = None if is_guest else payload.provider

    # 前置可用性检查：该 provider 实际会用的聊天客户端缺失 → HTTP 5031
    # （口径与 worker 主路径一致：缺省/未知 provider 回主槽位；只配命名档不再误 5031）
    if not chat_available(effective_provider):
        _note(503)
        return degraded_503("chat: 核心槽位未配置")

    # 会话归属前置校验（审计 P1-1）：登录用户传他人会话 id 一律 4041——不把他人
    # messages/summary 注入模型上下文，也不把回答写进他人会话。口径复用
    # api/conversations._scope（同一可见性只该有一套判据）；**存在但不可见=越权
    # 404，不存在=新建放行**（两段查询区分，避免误杀新会话首问）。
    if payload.conversation_id and identity is not None and identity.user_id:
        if not is_uuid(payload.conversation_id):
            _note(404)
            return JSONResponse(status_code=404,
                                content=error_body(EC_NOT_FOUND, "资源不存在"))
        from bidqa.application.chat import worker as worker_mod  # 与落库同源工厂（测试同缝）
        store = worker_mod.build_conversation_store()
        if store is not None:  # PG 不可用：跳过校验（问答能力型降级语义，与落库一致）
            viewer, machine = _scope(request)
            if store.get_conversation(payload.conversation_id, viewer_user_id=viewer,
                                      machine_domain=machine) is None \
                    and store.get_conversation(payload.conversation_id) is not None:
                _note(404)
                return JSONResponse(status_code=404,
                                    content=error_body(EC_NOT_FOUND, "资源不存在"))
    from bidqa.application.chat.worker import ChatJob, ChatJobError, run_chat_job

    _note(200)
    channel = SseChannel()
    trace_id = uuid.uuid4().hex[:12]  # 先于 job 生成：落库收尾结构与 SSE done 共用同一 id
    job = ChatJob(
        message=payload.message,
        conversation_id=payload.conversation_id,
        web_enabled=payload.web_enabled,
        thinking_enabled=payload.thinking_enabled,
        image_refs=payload.image_refs,
        provider=effective_provider,  # P1-27：游客一律 None=主槽位
        lang=payload.lang,
        thinking_model=settings.CHAT_THINKING_MODEL,
        plan_enabled=settings.AGENT_PLAN_ENABLED,      # WP5a 开关（能力型，默认开）
        reflect_enabled=settings.AGENT_REFLECT_ENABLED,
        is_guest=bool(identity and identity.role == ROLE_GUEST),
        user_id=(identity.user_id if identity else None),
        trace_id=trace_id,
    )
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
        done = compose_done_event(outcome.result, elapsed_ms=elapsed_ms,
                                  trace_id=trace_id, provider=payload.provider or "")
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
