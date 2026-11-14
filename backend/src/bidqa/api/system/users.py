"""B12 用户管理端点（admin GUI）：列表/建号/改密吊销/停用·启用 + 游客开关。

薄直调层（重构 C4）：store 语义单一入口在 auth/store（CLI `bidqa user` 同款），
本模块只做 HTTP 校验与错误码映射——按检查点①拍板，纯转发不建 application 用例。
admin-only 门禁由中间件按 /api/system 前缀统一执行。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bidqa.auth.factory import build_users_store
from bidqa.errors import (EC_CONFLICT, EC_NOT_FOUND, EC_SOURCE_DEGRADED,
                          EC_VALIDATION, error_body)

router = APIRouter()


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=4, max_length=128)
    role: Literal["admin", "user"]


class PasswordRequest(BaseModel):
    password: str = Field(min_length=4, max_length=128)


class GuestEnabledRequest(BaseModel):
    enabled: bool


def _store_or_503() -> Any:
    return build_users_store()


def _503_store() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content=error_body(EC_SOURCE_DEGRADED, "数据源降级/不可用",
                           detail={"source": "postgres: 用户体系不可用"}),
    )


@router.get("/api/system/users")
def system_users() -> Any:
    """用户列表（含每人活跃会话数，store.list_users 的 LEFT JOIN）。"""
    store = _store_or_503()
    if store is None:
        return _503_store()
    return {"users": store.list_users()}


@router.post("/api/system/users")
def system_users_create(payload: CreateUserRequest) -> Any:
    """建号；重名 → 409/4091（资源冲突，扩义后的 EC_CONFLICT）。"""
    store = _store_or_503()
    if store is None:
        return _503_store()
    uid = store.create_user(payload.username.strip(), payload.password, payload.role)
    if uid is None:
        return JSONResponse(
            status_code=409,
            content=error_body(EC_CONFLICT, "用户名已存在",
                               detail={"username": payload.username.strip()}))
    return {"created": True, "id": uid, "username": payload.username.strip(),
            "role": payload.role}


@router.post("/api/system/users/{username}/reset-password")
def system_user_reset_password(username: str, payload: PasswordRequest) -> Any:
    """改密并吊销该用户全部会话（store 层单一入口，CLI `user reset` 同款语义）。"""
    store = _store_or_503()
    if store is None:
        return _503_store()
    revoked = store.reset_password(username, payload.password)
    if revoked is None:
        return JSONResponse(status_code=404,
                            content=error_body(EC_NOT_FOUND, "资源不存在"))
    return {"reset": True, "sessions_revoked": revoked}


@router.post("/api/system/users/{username}/disable")
def system_user_disable(username: str, request: Request) -> Any:
    """停用账号（resolve 复核 → 既有会话即刻失效）；自停防呆 400/4001。"""
    identity = getattr(request.state, "identity", None)
    if (identity is not None and identity.user_id is not None
            and identity.username == username):
        return JSONResponse(status_code=400,
                            content=error_body(EC_VALIDATION, "不能停用自己"))
    store = _store_or_503()
    if store is None:
        return _503_store()
    if not store.set_disabled(username, True):
        return JSONResponse(status_code=404,
                            content=error_body(EC_NOT_FOUND, "资源不存在"))
    return {"disabled": True, "username": username}


@router.post("/api/system/users/{username}/enable")
def system_user_enable(username: str) -> Any:
    store = _store_or_503()
    if store is None:
        return _503_store()
    if not store.set_disabled(username, False):
        return JSONResponse(status_code=404,
                            content=error_body(EC_NOT_FOUND, "资源不存在"))
    return {"disabled": False, "username": username}


@router.post("/api/system/settings/guest-enabled")
def system_settings_guest_enabled(payload: GuestEnabledRequest) -> Any:
    """游客开关（即时生效：中间件逐请求复核 guest_enabled，在线游客一并被踢）。"""
    store = _store_or_503()
    if store is None:
        return _503_store()
    store.set_guest_enabled(payload.enabled)
    return {"guest_enabled": payload.enabled}
