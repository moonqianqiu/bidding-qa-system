"""rag 层工厂（重构 C3，从 service.py 归位）：嵌入/重排/检索器的惰性构建。

降级语义不变：EMBEDDING/RERANK 未配置 = None（BM25 灾备通道/跳过精排，
docs/01 §5）；Retriever 组装 = QdrantRepo + 嵌入 + 重排（检索域的跨件组装）。
"""

from __future__ import annotations

from functools import lru_cache

from bidqa.clients.siliconflow import EmbeddingClient, RerankClient
from bidqa.config import get_settings
from bidqa.database.factory import build_repo
from bidqa.rag.retrieve import Retriever


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


@lru_cache(maxsize=1)
def build_retriever() -> Retriever:
    return Retriever(build_repo(), build_embedding(), build_reranker())
