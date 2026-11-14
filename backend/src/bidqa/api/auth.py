"""用户体系端点（docs/01 §3.1）：登录 / 游客进入 / 当前身份 / 登出。

login/guest/me 在中间件豁免清单（登录引导，未登录可访问）；logout 需有效会话
（注销自身）。me 未登录返回 200 + authenticated:false（登录页渲染依据，
不产生 401 噪音）。store 不可用（PG 不可用）→ 503/5031（能力降级如实）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bidqa.auth.factory import build_users_store
from bidqa.auth.identity import MACHINE_USERNAME, ROLE_ADMIN, Identity
from bidqa.config import get_settings
from bidqa.errors import EC_FORBIDDEN, EC_SOURCE_DEGRADED, EC_UNAUTHORIZED, error_body

router = APIRouter()


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


def _store_or_503() -> Any:
    store = build_users_store()
    if store is None:
        return None
    return store


def _503() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content=error_body(EC_SOURCE_DEGRADED, "数据源降级/不可用",
                           detail={"source": "postgres: 用户体系不可用"}),
    )


@router.post("/api/auth/login")
def login(payload: LoginRequest, settings: Any = Depends(get_settings)) -> Any:
    """账号密码登录 → 发会话令牌；凭据错误统一 401/4011（不区分用户不存在，防枚举）。"""
    store = _store_or_503()
    if store is None:
        return _503()
    session = store.login(payload.username.strip(), payload.password,
                          settings.SESSION_TTL_HOURS)
    if session is None:
        return JSONResponse(status_code=401,
                            content=error_body(EC_UNAUTHORIZED, "用户名或密码错误"))
    return session


@router.post("/api/auth/guest")
def guest(settings: Any = Depends(get_settings)) -> Any:
    """游客进入（app_settings.guest_enabled 运行时开关；关闭=403/4031）。"""
    store = _store_or_503()
    if store is None:
        return _503()
    session = store.login_guest(settings.SESSION_TTL_HOURS)
    if session is None:
        return JSONResponse(status_code=403,
                            content=error_body(EC_FORBIDDEN, "游客模式已关闭"))
    return session


@router.get("/api/auth/me")
def me(request: Request, settings: Any = Depends(get_settings)) -> dict[str, Any]:
    """当前身份（豁免端点，中间件未解析身份——此处自查）。登录页据此决定渲染/跳转。"""
    out: dict[str, Any] = {"authenticated": False, "users_enabled": settings.USERS_ENABLED,
                           "guest_enabled": False}
    if not settings.USERS_ENABLED:
        return out  # 旧单机模式：无登录概念
    store = _store_or_503()
    if store is None:
        return out
    out["guest_enabled"] = store.guest_enabled()
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else ""
    identity: Identity | None = store.resolve(token) if token else None
    if identity is None and settings.auth_enabled and auth == f"Bearer {settings.API_AUTH_TOKEN}":
        identity = Identity(ROLE_ADMIN, None, MACHINE_USERNAME)  # 机器主 token 亦视为已登录
    if identity is not None:
        out.update({"authenticated": True, "username": identity.username,
                    "role": identity.role})
    return out


@router.post("/api/auth/logout")
def logout(request: Request) -> dict[str, Any]:
    """注销当前会话（中间件已验身份；机器主 token 无会话可删，幂等返回）。"""
    store = _store_or_503()
    if store is None:
        return {"logged_out": False}
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else ""
    return {"logged_out": bool(token and store.logout(token))}
