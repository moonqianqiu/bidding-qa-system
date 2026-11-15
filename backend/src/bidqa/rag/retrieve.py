"""混合召回 + 精排（docs/02 E1-E5；参数表 docs/01 §4.1 单一来源）。

E1 分类 → E2 嵌入（不可用则 BM25 灾备）→ E3 两路独立召回 + 客户端 RRF 融合
（k 由 classify 注入，见 database/rrf.py）→ E4 精排（不可用跳过）
→ E5 有界 FIFO 缓存（128 条，按插入顺序淘汰、命中不提升；只缓存成功结果，返回深拷贝）。

score 口径：hits[*].score 只在精排成功时是 0~1 的 relevance；bm25_fallback
与未精排的 hybrid 一律为 None（RRF 排名分/点积分不冒充相关度，取不到记
「不可用」，绝不记 0）。CRAG 与前端相关度条据此判「有没有可比置信度」。
"""

from __future__ import annotations

import math
import time
from copy import deepcopy
from dataclasses import dataclass, field

from qdrant_client import models

from bidqa.clients.siliconflow import DataSourceError, EmbeddingClient, RerankClient
from bidqa.database.qdrant import QdrantRepo
from bidqa.rag.bm25 import sparse_bm25
from bidqa.rag.classify import classify

PREFETCH_SIZE = 30        # prefetch_dense / prefetch_sparse
RERANK_CANDIDATES = 15    # 上限 30
FINAL_TOP_K = 5
CACHE_SIZE = 128


@dataclass
class Hit:
    id: str
    score: float | None
    source_type: str
    title: str
    text: str
    chapter: str = ""
    ref: str = ""
    page: int | None = None
    url: str = ""
    batch_id: str = ""

    def as_source(self) -> dict:
        return {
            "id": self.id, "source_type": self.source_type, "title": self.title,
            "text": self.text, "chapter": self.chapter, "ref": self.ref,
            "page": self.page, "url": self.url,
            "score": round(self.score, 4) if self.score is not None else None,
        }


@dataclass
class RetrievalResult:
    """一次检索的结果（缓存命中时整个对象原样返回，见 elapsed_ms 说明）。

    elapsed_ms 是首次（未命中缓存那次）检索的耗时；缓存命中时仍是旧值，
    不代表本次调用耗时。做延迟统计请在调用侧自行计时（ask.py 的 retrieve_ms 如此）；
    eval/smoke.py 的 latency_p95_ms 与 eval/ragas.py 已改为在调用侧自行计时，不再读本字段。

    rrf_k 是本次融合实际使用的 k（由 classify(question) 确定性产出，经
    query_hybrid(rrf_k=...) 注入）；bm25_fallback 单路不融合，该值仅为诊断。
    hits[*].score 见模块头口径：None 表示没有可比的相关度，绝不记 0。
    """

    hits: list[Hit]
    kind: str
    rrf_k: int
    mode: str                 # hybrid | bm25_fallback
    elapsed_ms: int           # 首次检索耗时，缓存命中时非本次
    degraded: list[str] = field(default_factory=list)


class _FIFO:
    """有界缓存：按插入顺序淘汰最早项，get 命中不提升位置（语义=FIFO，不是 LRU）。

    仅正名使命名与实现一致，不改动淘汰策略——命中率影响未经测量，勿顺手改成真 LRU。
    """

    def __init__(self, size: int = CACHE_SIZE):
        self.size = size
        self._data: dict = {}

    def get(self, key):
        if key in self._data:
            return deepcopy(self._data[key])
        return None

    def put(self, key, value) -> None:
        self._data[key] = value
        if len(self._data) > self.size:
            self._data.pop(next(iter(self._data)))


class Retriever:
    def __init__(self, repo: QdrantRepo, embedding: EmbeddingClient | None,
                 reranker: RerankClient | None):
        self.repo = repo
        self.embedding = embedding
        self.reranker = reranker
        self._cache = _FIFO()

    def retrieve(self, question: str, top_k: int = FINAL_TOP_K) -> RetrievalResult:
        key = (question, top_k)
        if (cached := self._cache.get(key)) is not None:
            return cached  # cached.elapsed_ms 是首次检索耗时，非本次（见类 docstring）
        t0 = time.perf_counter()
        degraded: list[str] = []
        kind, rrf_k = classify(question)

        q_dense: list[float] | None = None
        q_sparse = None
        if self.embedding is not None:
            try:
                q_dense, q_sparse = self.embedding.embed_query(question)
            except DataSourceError as exc:
                degraded.append(f"embedding: {exc}")
        if q_dense is None and q_sparse is None:
            mode = "bm25_fallback"
            points = self.repo.query_bm25(sparse_bm25(question), limit=top_k)
            hits = [self._to_hit(p, None) for p in points]
        else:
            mode = "hybrid"
            points = self.repo.query_hybrid(q_dense, q_sparse, rrf_k=rrf_k,
                                            prefetch_size=PREFETCH_SIZE,
                                            limit=max(RERANK_CANDIDATES, top_k))
            hits = [self._to_hit(p, None) for p in points]
            if self.reranker is not None and len(hits) > 1:
                try:
                    ranked = self.reranker.rerank(
                        question, [h.text for h in hits], top_n=top_k,
                    )
                    hits = [hits[i] for i, _ in ranked]
                    for h, (_, score) in zip(hits, ranked):
                        h.score = float(score) if math.isfinite(float(score)) else None
                except DataSourceError as exc:
                    degraded.append(f"rerank: {exc}")
                    hits = hits[:top_k]
            else:
                hits = hits[:top_k]

        result = RetrievalResult(hits=hits[:top_k], kind=kind, rrf_k=rrf_k, mode=mode,
                                 elapsed_ms=int((time.perf_counter() - t0) * 1000),
                                 degraded=degraded)
        if not degraded and hits:
            # 只缓存成功结果（硬约束 2；审计 P1-10：降级/空结果曾无条件入缓存，
            # 故障恢复后仍返旧结果）；存深拷贝隔离调用方变异（CRAG 就地 append
            # degraded 曾把降级注记写进缓存）
            self._cache.put(key, deepcopy(result))
        return result

    @staticmethod
    def _to_hit(point: models.ScoredPoint, score: float | None) -> Hit:
        p = point.payload or {}
        return Hit(
            id=str(point.id), score=score,
            source_type=str(p.get("source_type", "")), title=str(p.get("title", "")),
            text=str(p.get("text", "")), chapter=str(p.get("chapter", "")),
            ref=str(p.get("ref", "")), page=p.get("page"), url=str(p.get("url", "")),
            batch_id=str(p.get("batch_id", "")),
        )
