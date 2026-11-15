"""database 层工厂（重构 C2/C3，从 service.py 归位）：三库连接与存储的惰性构建。

能力降级语义不变：POSTGRES/NEO4J 未配置或不可达 = None（health 如实显示）。
build_pg 正缓存单例 + **失败负缓存短窗口**（审计 P1-2：lru_cache 曾把首次失败
永久缓存成 None，PG 恢复后不重启仍全量降级）——窗口 10s 内不重试连接（避免每
请求都付 5s connect timeout），窗口过后自动重连。存储对象构造零 IO，随查随建。
"""

from __future__ import annotations

import time
from functools import lru_cache

from bidqa.config import get_settings
from bidqa.database.bp_repo import BpRepo
from bidqa.database.connection import PgConn
from bidqa.database.conversations import ConversationStore
from bidqa.database.feedback import FeedbackStore
from bidqa.database.neo4j import NeoClient
from bidqa.database.qdrant import QdrantRepo

_PG_RETRY_SECONDS = 10.0
_pg_cache: PgConn | None = None
_pg_failed_at: float = 0.0


def build_pg() -> PgConn | None:
    """PG 连接（schema 幂等建表失败=降级 None，负缓存 10s 后自动重试）。"""
    global _pg_cache, _pg_failed_at
    if _pg_cache is not None:
        return _pg_cache
    now = time.monotonic()
    if now - _pg_failed_at < _PG_RETRY_SECONDS:
        return None
    s = get_settings()
    if not s.has_postgres:
        return None
    conn = PgConn(s.POSTGRES_HOST, s.POSTGRES_PORT, s.POSTGRES_DB,
                  s.POSTGRES_USER, s.POSTGRES_PASSWORD)
    try:
        conn.ensure_schema()
    except Exception:
        _pg_failed_at = now
        return None
    _pg_cache = conn
    return conn


def clear_pg_cache() -> None:
    """测试隔离口（conftest 用）：清正缓存与负缓存标记。"""
    global _pg_cache, _pg_failed_at
    _pg_cache = None
    _pg_failed_at = 0.0


def build_bp_repo() -> BpRepo | None:
    conn = build_pg()
    return BpRepo(conn) if conn is not None else None


def build_conversation_store() -> ConversationStore | None:
    conn = build_pg()
    return ConversationStore(conn) if conn is not None else None


def build_feedback_store() -> FeedbackStore | None:
    conn = build_pg()
    return FeedbackStore(conn) if conn is not None else None


_neo_cache: NeoClient | None = None


def build_neo() -> NeoClient | None:
    """Neo4j 连接（连通性失败=降级 None，health 如实显示）。"""
    global _neo_cache
    if _neo_cache is not None:
        return _neo_cache
    s = get_settings()
    if not s.has_neo4j:
        return None
    client = NeoClient(s.NEO4J_URI, s.NEO4J_USER, s.NEO4J_PASSWORD)
    if not client.ping():
        return None
    _neo_cache = client
    return client


def close_pools() -> None:
    """停机钩子（lifespan 四步④，审计 P1-8）：关闭进程内长连接（PG/Neo4j）。"""
    global _pg_cache, _neo_cache
    if _pg_cache is not None:
        _pg_cache.close()
        _pg_cache = None
    if _neo_cache is not None:
        _neo_cache.close()
        _neo_cache = None


@lru_cache(maxsize=1)
def build_repo() -> QdrantRepo:
    """Qdrant 向量库仓库（核心槽位，缺失=启动失败，docs/01 §5）。"""
    s = get_settings()
    return QdrantRepo(url=s.QDRANT_URL, collection=s.QDRANT_COLLECTION,
                      api_key=s.QDRANT_API_KEY)
