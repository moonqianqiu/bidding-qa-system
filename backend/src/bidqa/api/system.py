"""H2 系统监控端点（docs/02）：资源快照（5s 缓存）+ 内存日志环形缓冲。

脱敏先具体后通用：API key 形态先掩码；取不到的资源项记「不可用」绝不记 0。
单进程约定（docs/01 §6）——计数与日志缓冲均为进程内。
A15/B11/B12（docs/02）：/api/system/console 三库规模聚合 + 用户管理 + 游客开关 +
全库会话管理（admin-only，中间件前缀门禁；全库端点是聊天页可见域的独立旁路，
不改动 conversations.py 的三元组）。
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bidqa.auth.identity import ROLE_ADMIN
from bidqa.config import get_settings
from bidqa.database.postgres import is_uuid
from bidqa.errors import (EC_CONFLICT, EC_NOT_FOUND, EC_SOURCE_DEGRADED,
                          EC_VALIDATION, error_body)
from bidqa.service import build_neo, build_pg, build_repo, build_users_store

router = APIRouter()

_started_at = time.monotonic()
_request_stats: dict[str, dict[str, int]] = {}
_log_buffer: deque[dict[str, str]] = deque(maxlen=500)
_metrics_cache: tuple[float, dict] | None = None
_METRICS_TTL = 5.0

_KEY_RE = re.compile(r"(sk-[A-Za-z0-9]{4})[A-Za-z0-9-]+([A-Za-z0-9]{4})")


def mask_secrets(text: str) -> str:
    """脱敏先具体后通用（docs/02 H 域）：先 API key 形态，其余规则按需追加。"""
    return _KEY_RE.sub(r"\1****\2", text)


class MemoryLogHandler(logging.Handler):
    """内存环形日志缓冲（单进程）；写入前脱敏。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = mask_secrets(self.format(record))
        except Exception:
            return
        _log_buffer.append({"ts": time.strftime("%H:%M:%S"),
                            "level": record.levelname,
                            "name": record.name, "message": message[:500]})


def setup_log_buffer(level: int = logging.INFO) -> None:
    handler = MemoryLogHandler()
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level)
    # 应用级日志同步落 stderr（start.sh 2>&1 → backend.log）。此前根 logger 只有内存环，
    # 「上游流式响应为空」等 warning 在 backend.log 里永远看不到（2026-11-08 实证修复）。
    # uvicorn 自有 handler 不传播到 root，不会重复输出。幂等：重复 setup 不叠 handler。
    bidqa = logging.getLogger("bidqa")
    if not any(isinstance(h, logging.StreamHandler) for h in bidqa.handlers):
        stream = logging.StreamHandler()  # 默认 stderr
        stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        bidqa.addHandler(stream)
    bidqa.setLevel(level)


def note_request(path: str, status_code: int) -> None:
    entry = _request_stats.setdefault(path, {"count": 0, "errors": 0})
    entry["count"] += 1
    if status_code >= 500:
        entry["errors"] += 1


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
        "requests": _request_stats,
        "log_buffer_size": len(_log_buffer),
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


@router.get("/api/system/providers")
def system_providers() -> dict[str, Any]:
    """A12/C8 档位列表（前端下拉用；只给名字与模型，绝不回传 key）。"""
    settings = get_settings()
    profiles = [{"name": name, "model": p.get("model", "")}
                for name, p in settings.chat_profiles().items()
                if p.get("base_url") and p.get("api_key") and p.get("model")]
    return {"default_model": settings.CHAT_MODEL, "profiles": profiles}


@router.get("/api/system/logs")
def system_logs(level: str = Query(default="INFO"),
                q: str = Query(default="", max_length=100)) -> Any:
    order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
    min_level = order.get(level.upper(), 20)
    logs = [entry for entry in _log_buffer
            if order.get(entry["level"], 20) >= min_level
            and (not q or q.lower() in entry["message"].lower())]
    return {"logs": list(logs)[-200:], "total": len(logs), "buffered": len(_log_buffer)}


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
    """用户列表（含每人活跃会话数，auth_core.list_users 的 LEFT JOIN）。"""
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
