"""会话 CRUD + 反馈端点（docs/01 §3.1 B1；A7 反馈闭环的服务端）。

标识符非法/不存在一律 404（error handler → 4041）；PG 未配置 → 5031（能力降级如实）。
会话隔离（用户体系）：按 request.state.identity 过滤可见域——普通用户仅本人；
人类 admin 含机器域（NULL）；机器主 token 仅机器域；越权一律 404（不泄漏存在性）；
identity 缺失（legacy 开发模式）=不过滤（旧行为）。游客在中间件已被拦（403）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bidqa.application.chat.online import record_feedback
from bidqa.auth.identity import ROLE_ADMIN, ROLE_GUEST
from bidqa.config import get_settings
from bidqa.database.conversations import is_uuid
from bidqa.database.factory import build_conversation_store, build_feedback_store
from bidqa.api.deps import degraded_503
from bidqa.errors import EC_NOT_FOUND, error_body

router = APIRouter()


def _store_or_5031() -> Any:
    return build_conversation_store()


def _5031() -> JSONResponse:
    return degraded_503("postgres: 未配置或不可用")


def _scope(request: Request) -> tuple[str | None, bool]:
    """identity → (viewer_user_id, machine_domain)；None（legacy）=不过滤（旧行为）。"""
    identity = getattr(request.state, "identity", None)
    if identity is None:
        return None, False
    if identity.user_id is None:
        return None, True  # 机器域（机器主 token；游客已被中间件拦）
    return identity.user_id, identity.role == ROLE_ADMIN


@router.get("/api/conversations")
def list_conversations(request: Request) -> dict[str, Any]:
    store = _store_or_5031()
    if store is None:
        return _5031()
    viewer, machine = _scope(request)
    return {"conversations": store.list_conversations(viewer_user_id=viewer,
                                                       machine_domain=machine)}


@router.get("/api/conversations/{conv_id}")
def get_conversation(conv_id: str, request: Request) -> Any:
    if not is_uuid(conv_id):
        return JSONResponse(status_code=404,
                            content=error_body(EC_NOT_FOUND, "资源不存在"))
    store = _store_or_5031()
    if store is None:
        return _5031()
    viewer, machine = _scope(request)
    row = store.get_conversation(conv_id, viewer_user_id=viewer, machine_domain=machine)
    if row is None:
        return JSONResponse(status_code=404,
                            content=error_body(EC_NOT_FOUND, "资源不存在"))
    return row


@router.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: str, request: Request) -> Any:
    if not is_uuid(conv_id):
        return JSONResponse(status_code=404,
                            content=error_body(EC_NOT_FOUND, "资源不存在"))
    store = _store_or_5031()
    if store is None:
        return _5031()
    viewer, machine = _scope(request)
    if not store.delete_conversation(conv_id, viewer_user_id=viewer,
                                     machine_domain=machine):
        return JSONResponse(status_code=404,
                            content=error_body(EC_NOT_FOUND, "资源不存在"))
    return {"deleted": conv_id}


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)


@router.patch("/api/conversations/{conv_id}")
def rename_conversation(conv_id: str, payload: RenameRequest, request: Request) -> Any:
    """会话重命名（WP4 前端小件）：非空标题；非法/不存在/越权 404；PG 缺失 5031。"""
    if not is_uuid(conv_id):
        return JSONResponse(status_code=404,
                            content=error_body(EC_NOT_FOUND, "资源不存在"))
    store = _store_or_5031()
    if store is None:
        return _5031()
    viewer, machine = _scope(request)
    if not store.rename_conversation(conv_id, payload.title.strip(),
                                     viewer_user_id=viewer, machine_domain=machine):
        return JSONResponse(status_code=404,
                            content=error_body(EC_NOT_FOUND, "资源不存在"))
    return {"id": conv_id, "title": payload.title.strip()}


@router.get("/api/conversations/{conv_id}/feedback")
def list_conversation_feedback(conv_id: str, request: Request) -> Any:
    """会话反馈态回查（A7 债②）：👍/👎 按问题回读，刷新后前端还原反馈态。
    归属校验与会话详情同口径（越权一律 404，不泄漏存在性）。"""
    if not is_uuid(conv_id):
        return JSONResponse(status_code=404,
                            content=error_body(EC_NOT_FOUND, "资源不存在"))
    store = _store_or_5031()
    if store is None:
        return _5031()
    viewer, machine = _scope(request)
    if store.get_conversation(conv_id, viewer_user_id=viewer,
                              machine_domain=machine) is None:
        return JSONResponse(status_code=404,
                            content=error_body(EC_NOT_FOUND, "资源不存在"))
    fb = build_feedback_store()
    if fb is None:
        return _5031()
    return {"ratings": fb.ratings_for_conversation(conv_id)}


class FeedbackRequest(BaseModel):
    conversation_id: str | None = None
    question: str = Field(min_length=1, max_length=4000)
    rating: str = Field(pattern="^(up|down)$")
    sources: list[dict] = Field(default_factory=list, max_length=20)
    trace_id: str = Field(default="", max_length=64)
    sources_belongs_to_round: bool | None = None  # A7「来源是否属于本轮」标记
    answer: str = Field(default="", max_length=8000)  # G4 点踩必评用（忠实度判分）


@router.post("/api/feedback")
def create_feedback(payload: FeedbackRequest, request: Request,
                    settings: Any = Depends(get_settings)) -> Any:  # 404/5031 分支返 JSONResponse
    fb = build_feedback_store()
    if fb is None:
        return _5031()
    identity = getattr(request.state, "identity", None)
    conv_id = payload.conversation_id
    if identity is not None and identity.role == ROLE_GUEST:
        conv_id = None  # 游客会话只在进程内：conv id 一律不落（防拿他人会话 id 伪造反馈/触发判分，审计 P1-1）
    if conv_id:
        if not is_uuid(conv_id):
            return JSONResponse(status_code=404,
                                content=error_body(EC_NOT_FOUND, "资源不存在"))
        if identity is not None and identity.user_id is not None:
            # 登录用户：归属校验与 GET 回查同口径（他人会话 404 不泄漏存在性）
            store = build_conversation_store()
            viewer, machine = _scope(request)
            if store is not None and store.get_conversation(
                    conv_id, viewer_user_id=viewer, machine_domain=machine) is None:
                return JSONResponse(status_code=404,
                                    content=error_body(EC_NOT_FOUND, "资源不存在"))
    feedback_id = fb.insert_feedback(
        conv_id, payload.question, payload.rating,
        [{"id": s.get("id"), "title": s.get("title", "")} for s in payload.sources],
        payload.trace_id)
    if payload.rating == "down":  # G4 点踩必评：忠实度单维（60s 去重；旁路不抛错）
        record_feedback(settings, trace_id=payload.trace_id,
                        question=payload.question, answer=payload.answer,
                        sources=list(payload.sources))
    return {"id": feedback_id, "rating": payload.rating}
