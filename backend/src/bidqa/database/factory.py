"""database 层工厂（重构 C2/C3，从 service.py 归位）：三库连接与存储的惰性构建。

能力降级语义不变：POSTGRES/NEO4J 未配置或不可达 = None（health 如实显示）；
存储工厂随 PgConn 降级。lru_cache 单例（进程内复用连接，单进程约定 docs/01 §6）。
"""

from __future__ import annotations

from functools import lru_cache

from bidqa.config import get_settings
from bidqa.database.bp_repo import BpRepo
from bidqa.database.connection import PgConn
from bidqa.database.conversations import ConversationStore
from bidqa.database.feedback import FeedbackStore
from bidqa.database.neo4j import NeoClient
from bidqa.database.qdrant import QdrantRepo


@lru_cache(maxsize=1)
def build_pg() -> PgConn | None:
    """PG 连接（schema 幂等建表失败=降级 None，health 如实显示）。"""
    s = get_settings()
    if not s.has_postgres:
        return None
    conn = PgConn(s.POSTGRES_HOST, s.POSTGRES_PORT, s.POSTGRES_DB,
                  s.POSTGRES_USER, s.POSTGRES_PASSWORD)
    try:
        conn.ensure_schema()
    except Exception:
        return None
    return conn


@lru_cache(maxsize=1)
def build_bp_repo() -> BpRepo | None:
    conn = build_pg()
    return BpRepo(conn) if conn is not None else None


@lru_cache(maxsize=1)
def build_conversation_store() -> ConversationStore | None:
    conn = build_pg()
    return ConversationStore(conn) if conn is not None else None


@lru_cache(maxsize=1)
def build_feedback_store() -> FeedbackStore | None:
    conn = build_pg()
    return FeedbackStore(conn) if conn is not None else None


@lru_cache(maxsize=1)
def build_neo() -> NeoClient | None:
    """Neo4j 连接（连通性失败=降级 None，health 如实显示）。"""
    s = get_settings()
    if not s.has_neo4j:
        return None
    client = NeoClient(s.NEO4J_URI, s.NEO4J_USER, s.NEO4J_PASSWORD)
    return client if client.ping() else None


@lru_cache(maxsize=1)
def build_repo() -> QdrantRepo:
    """Qdrant 向量库仓库（核心槽位，缺失=启动失败，docs/01 §5）。"""
    s = get_settings()
    return QdrantRepo(url=s.QDRANT_URL, collection=s.QDRANT_COLLECTION,
                      api_key=s.QDRANT_API_KEY)
