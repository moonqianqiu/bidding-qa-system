"""服务装配（settings → 客户端/仓库实例）。按能力槽位惰性构建，缺失即 None（降级）。

无参 + lru_cache（Settings 不可哈希，不能作缓存键）；测试用 cache_clear 复位。
"""

from __future__ import annotations

from functools import lru_cache

from bidqa.clients.exa import ExaClient
from bidqa.clients.llm import ChatClient
from bidqa.clients.siliconflow import EmbeddingClient, RerankClient
from bidqa.clients.tavily import TavilyClient
from bidqa.config import get_settings
from bidqa.database.neo4j import NeoClient
from bidqa.database.postgres import PgClient
from bidqa.database.qdrant import QdrantRepo
from bidqa.rag.retrieve import Retriever


@lru_cache(maxsize=1)
def build_repo() -> QdrantRepo:
    s = get_settings()
    return QdrantRepo(url=s.QDRANT_URL, collection=s.QDRANT_COLLECTION, api_key=s.QDRANT_API_KEY)


@lru_cache(maxsize=1)
def build_embedding() -> EmbeddingClient | None:
    s = get_settings()
    if not s.has_embedding:
        return None
    return EmbeddingClient(s.EMBEDDING_BASE_URL, s.EMBEDDING_API_KEY, s.EMBEDDING_MODEL)


@lru_cache(maxsize=1)
def build_reranker() -> RerankClient | None:
    s = get_settings()
    if not s.has_rerank:
        return None
    return RerankClient(s.RERANK_BASE_URL, s.RERANK_API_KEY, s.RERANK_MODEL)


# 主槽位客户端工厂已归 clients/chat_factory（重构 C1 pilot）——此处兼容再导出，
# ask/health/cli 等旧导入方暂不受影响；C3 随 application/chat 落地后移除本行。
from bidqa.clients.chat_factory import build_chat, build_chat_for  # noqa: F401


@lru_cache(maxsize=1)
def build_retriever() -> Retriever:
    return Retriever(build_repo(), build_embedding(), build_reranker())


@lru_cache(maxsize=1)
def build_pg() -> PgClient | None:
    """PG 客户端（schema 幂等建表失败=降级 None，health 如实显示）。"""
    s = get_settings()
    if not s.has_postgres:
        return None
    client = PgClient(s.POSTGRES_HOST, s.POSTGRES_PORT, s.POSTGRES_DB,
                      s.POSTGRES_USER, s.POSTGRES_PASSWORD)
    try:
        client.ensure_schema()
    except Exception:
        return None
    return client


@lru_cache(maxsize=1)
def build_neo() -> NeoClient | None:
    """Neo4j 客户端（连通性失败=降级 None，health 如实显示）。"""
    s = get_settings()
    if not s.has_neo4j:
        return None
    client = NeoClient(s.NEO4J_URI, s.NEO4J_USER, s.NEO4J_PASSWORD)
    return client if client.ping() else None


@lru_cache(maxsize=1)
def build_web_clients() -> dict[str, TavilyClient | ExaClient]:
    """联网检索客户端按 WEB_SEARCH_PROVIDERS 名册构建（名册∩key；空 dict=无联网，D4 工具不注册）。"""
    s = get_settings()
    clients: dict[str, TavilyClient | ExaClient] = {}
    for provider in s.web_search_providers():
        clients[provider] = (TavilyClient(s.TAVILY_API_KEY) if provider == "tavily"
                             else ExaClient(s.EXA_API_KEY))
    return clients


@lru_cache(maxsize=1)
def build_vision() -> ChatClient | None:
    """视觉槽位客户端（未配置=降级 None，/api/vision 5031；同一 OpenAI-compatible 适配器）。"""
    s = get_settings()
    if not s.has_vision:
        return None
    return ChatClient(s.VISION_BASE_URL, s.VISION_API_KEY, s.VISION_MODEL)


@lru_cache(maxsize=1)
def build_users_store() -> "UsersStore | None":
    """用户体系存储（docs/01 §3.1）：PG 不可用=降级 None（鉴权中间件按降级语义放行/拒绝）；
    成功则首次构建时种子管理员（users 空表才种，幂等）。"""
    pg = build_pg()
    if pg is None:
        return None
    from bidqa.auth.store import UsersStore

    s = get_settings()
    store = UsersStore(pg)
    if store.seed_if_empty(s.ADMIN_USERNAME, s.ADMIN_PASSWORD):
        import logging

        if s.ADMIN_PASSWORD == "admin123":
            logging.getLogger("bidqa").warning(
                "管理员 %s 以默认口令种子（admin/admin123），请尽快 `bidqa user reset` 改密",
                s.ADMIN_USERNAME)
    return store

