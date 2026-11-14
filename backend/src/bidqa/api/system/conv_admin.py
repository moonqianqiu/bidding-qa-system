"""B12 全库会话管理端点（admin 旁路）：列表/删除/空壳清理。

独立旁路——聊天页可见域三元组零改动；存储动作在 database/conversations。
dry_run 缺省 true=只预览（prune --dry-run 惯例）；10 分钟保护窗防误删
刚流式完成的活跃会话（判定逻辑在 store 层单一实现）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from bidqa.database.conversations import is_uuid
from bidqa.database.factory import build_conversation_store
from bidqa.api.deps import degraded_503
from bidqa.errors import EC_NOT_FOUND, error_body

router = APIRouter()


def _5031() -> JSONResponse:
    return degraded_503("postgres: 未配置或不可用")


class PurgeRequest(BaseModel):
    dry_run: bool = True  # 先预览再删（prune --dry-run 惯例）


@router.get("/api/system/conversations")
def system_conversations(limit: int = Query(default=200, ge=1, le=500),
                         q: str = Query(default="", max_length=100)) -> Any:
    """全库列表（属主 JOIN users；q 对 title ILIKE 过滤）——admin 管理视图。"""
    store = build_conversation_store()
    if store is None:
        return _5031()
    return {"conversations": store.list_all_conversations(limit=limit, q=q.strip())}


@router.delete("/api/system/conversations/{conv_id}")
def system_conversation_delete(conv_id: str) -> Any:
    """admin 旁路删（复用 delete_conversation 无参调用=不过滤）；不存在 → 404/4041。"""
    store = build_conversation_store()
    if store is None:
        return _5031()
    if not is_uuid(conv_id):
        return JSONResponse(status_code=404,
                            content=error_body(EC_NOT_FOUND, "资源不存在"))
    if not store.delete_conversation(conv_id):
        return JSONResponse(status_code=404,
                            content=error_body(EC_NOT_FOUND, "资源不存在"))
    return {"deleted": True, "id": conv_id}


@router.post("/api/system/conversations/purge-empty")
def system_conversations_purge(payload: PurgeRequest) -> Any:
    """空壳清理：无 assistant 回复且超出 10 分钟保护窗（防误删刚流式完成的会话）。
    dry_run=true（默认）只预览 {count, samples≤20}；false 才删并返回 {deleted}。"""
    store = build_conversation_store()
    if store is None:
        return _5031()
    if payload.dry_run:
        stats = store.empty_conversations()
        return {"dry_run": True, **stats}
    deleted = store.purge_empty_conversations()
    return {"dry_run": False, "deleted": deleted}
