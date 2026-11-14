"""H2 系统监控与管理端点（docs/02）：metrics/logs + console 聚合 + 用户管理 +
游客开关 + 全库会话管理（A15/B11/B12）+ providers 名册聚合出口。

观测状态（请求计数/日志环/脱敏）已归 obs/logs（重构 C1 pilot）——本模块只做
端点与缓存，不再承载观测状态，也不再被业务路由反向依赖（test_layering R4）。
单进程约定（docs/01 §6）——各缓存均为进程内。admin-only 门禁由中间件按
/api/system 前缀统一执行；全库会话端点是聊天页可见域的独立旁路，不改动
conversations.py 的三元组。C4 将按 console/users/conv_admin 继续拆分本文件。
"""

from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bidqa.api.system.providers import router as _providers_router
from bidqa.config import get_settings
from bidqa.database.postgres import is_uuid
from bidqa.errors import (EC_CONFLICT, EC_NOT_FOUND, EC_SOURCE_DEGRADED,
                          EC_VALIDATION, error_body)
from bidqa.obs.logs import log_buffer, request_stats
from bidqa.service import build_neo, build_pg, build_repo, build_users_store

router = APIRouter()
router.include_router(_providers_router)  # providers：纯配置只读视图（独立模块见同目录）

_started_at = time.monotonic()
_metrics_cache: tuple[float, dict] | None = None
_METRICS_TTL = 5.0


@router.get("/api/system/metrics")
def system_metrics() -> dict[str, Any]:
    global _metrics_cache
    now = time.monotonic()
    if _metrics_cache and now - _metrics_cache[0] < _METRICS_TTL:
        return _metrics_cache[1]

    settings = get_settings()
    resources: dict[str, Any] = {"note": "进程内快照"}
    try:
        import os

        proc = __import__("psutil", fromlist=["Process"]).Process()
        resources["cpu_percent"] = proc.cpu_percent(interval=0.05)
        mem = proc.memory_info()
        resources["rss_mb"] = round(mem.rss / 1024 / 1024, 1)
    except Exception:
        resources["cpu_percent"] = "不可用"  # 取不到记「不可用」，绝不记 0
        resources["rss_mb"] = "不可用"
    try:
        import shutil

        usage = shutil.disk_usage(settings.QDRANT_URL.split("//")[-1] or ".")
        resources["disk_free_gb"] = round(usage.free / 1024**3, 1)
    except Exception:
        resources["disk_free_gb"] = "不可用"

    metrics = {
        "uptime_s": int(now - _started_at),
        "requests": request_stats,
        "log_buffer_size": len(log_buffer),
        "resources": resources,
        "online_eval": _online_stats(),
    }
    _metrics_cache = (now, metrics)
    return metrics


def _online_stats() -> dict[str, Any]:
    """G4 在线评测计数（模块不可用 = 全 0 态，不抛错）。"""
    try:
        from bidqa.eval.online import get_online
        settings = get_settings()
        return {"enabled": bool(settings.ONLINE_EVAL_ENABLED),
                "sample_rate": settings.ONLINE_EVAL_SAMPLE_RATE,
                **get_online().stats()}
    except Exception:
        return {"enabled": False, "sample_rate": 0.0}


@router.get("/api/system/logs")
def system_logs(level: str = Query(default="INFO"),
                q: str = Query(default="", max_length=100)) -> Any:
    order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
    min_level = order.get(level.upper(), 20)
    logs = [entry for entry in log_buffer
            if order.get(entry["level"], 20) >= min_level
            and (not q or q.lower() in entry["message"].lower())]
    return {"logs": list(logs)[-200:], "total": len(logs), "buffered": len(log_buffer)}


# ══ A15/B11 控制台聚合（admin-only；KPI 三库规模一端点取齐，30s 缓存 health 同款）══

_console_cache: tuple[float, dict] | None = None
_CONSOLE_TTL = 30.0

_UNAVAILABLE = {"status": "不可用"}  # 硬约束 1：取不到记「不可用」，绝不记 0


@router.get("/api/system/console")
def system_console() -> dict[str, Any]:
    """三库规模聚合：pg{bidding_procurement, conversations, feedback, users} +
    qdrant{vectors, collection} + neo4j{labels 六类, total}。任一库挂 → 该节
    {"status": "不可用"}，其余节照常（诚实降级，不整页报废）。"""
    global _console_cache
    now = time.monotonic()
    if _console_cache and now - _console_cache[0] < _CONSOLE_TTL:
        return _console_cache[1]
    data: dict[str, Any] = {"pg": _pg_counts(), "qdrant": _qdrant_counts(),
                            "neo4j": _neo_counts()}
    _console_cache = (now, data)
    return data


def _pg_counts() -> dict[str, Any]:
    try:
        pg = build_pg()
        if pg is None:
            raise RuntimeError("未配置或不可用")
        conn = pg.connection()
        out: dict[str, Any] = {
            table: int(conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"])
            for table in ("bidding_procurement", "conversations", "feedback")}
        store = build_users_store()
        out["users"] = store.count_users() if store else "不可用"
        return out
    except Exception:
        return dict(_UNAVAILABLE)


def _qdrant_counts() -> dict[str, Any]:
    try:
        repo = build_repo()
        return {"vectors": int(repo.count()),
                "collection": get_settings().QDRANT_COLLECTION}
    except Exception:
        return dict(_UNAVAILABLE)


def _neo_counts() -> dict[str, Any]:
    try:
        neo = build_neo()
        if neo is None:
            raise RuntimeError("未配置或不可用")
        labels = neo.counts()
        return {"labels": labels, "total": sum(int(v) for v in labels.values())}
    except Exception:
        return dict(_UNAVAILABLE)


# ══ B12 管理端点共用小件 ══

def _store_or_503() -> Any:
    return build_users_store()


def _503_store() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content=error_body(EC_SOURCE_DEGRADED, "数据源降级/不可用",
                           detail={"source": "postgres: 用户体系不可用"}),
    )


def _pg_or_5031() -> Any:
    return build_pg()


def _5031() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content=error_body(EC_SOURCE_DEGRADED, "数据源降级/不可用",
                           detail={"source": "postgres: 未配置或不可用"}),
    )


def _identity(request: Request) -> Any:
    return getattr(request.state, "identity", None)


# ══ B12 用户管理（admin GUI；CLI `bidqa user` 的端点化，语义同款）══

class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=4, max_length=128)
    role: Literal["admin", "user"]


class PasswordRequest(BaseModel):
    password: str = Field(min_length=4, max_length=128)


class GuestEnabledRequest(BaseModel):
    enabled: bool


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
    identity = _identity(request)
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


# ══ B12 全库会话管理（独立旁路端点；聊天页可见域三元组零改动）══

class PurgeRequest(BaseModel):
    dry_run: bool = True  # 先预览再删（prune --dry-run 惯例）


@router.get("/api/system/conversations")
def system_conversations(limit: int = Query(default=200, ge=1, le=500),
                         q: str = Query(default="", max_length=100)) -> Any:
    """全库列表（属主 JOIN users；q 对 title ILIKE 过滤）——admin 管理视图。"""
    pg = _pg_or_5031()
    if pg is None:
        return _5031()
    return {"conversations": pg.list_all_conversations(limit=limit, q=q.strip())}


@router.delete("/api/system/conversations/{conv_id}")
def system_conversation_delete(conv_id: str) -> Any:
    """admin 旁路删（复用 delete_conversation 无参调用=不过滤）；不存在 → 404/4041。"""
    pg = _pg_or_5031()
    if pg is None:
        return _5031()
    if not is_uuid(conv_id):
        return JSONResponse(status_code=404,
                            content=error_body(EC_NOT_FOUND, "资源不存在"))
    if not pg.delete_conversation(conv_id):
        return JSONResponse(status_code=404,
                            content=error_body(EC_NOT_FOUND, "资源不存在"))
    return {"deleted": True, "id": conv_id}


@router.post("/api/system/conversations/purge-empty")
def system_conversations_purge(payload: PurgeRequest) -> Any:
    """空壳清理：无 assistant 回复且超出 10 分钟保护窗（防误删刚流式完成的会话）。
    dry_run=true（默认）只预览 {count, samples≤20}；false 才删并返回 {deleted}。"""
    pg = _pg_or_5031()
    if pg is None:
        return _5031()
    if payload.dry_run:
        stats = pg.empty_conversations()
        return {"dry_run": True, **stats}
    deleted = pg.purge_empty_conversations()
    return {"dry_run": False, "deleted": deleted}
