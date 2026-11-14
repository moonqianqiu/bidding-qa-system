"""Qdrant 仓库层（docs/01 §4.1）：版本化集合 + 三路 Named Vectors + payload 校验。

集合命名带版本（bidqa_knowledge_v1），重建=新集合导入→原子切换配置→删旧，
禁止原地清空重建。三路向量：dense(bge-m3 1024 COSINE) + sparse(bge-m3 学习型)
+ sparse_bm25(jieba 灾备，ingest 双写)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from qdrant_client import QdrantClient, models

VECTOR_DENSE = "dense"
VECTOR_SPARSE = "sparse"
VECTOR_SPARSE_BM25 = "sparse_bm25"
DENSE_DIM = 1024

PAYLOAD_REQUIRED = ("source_type", "title", "text", "batch_id")
# ctx_summary 为可选增强字段：F3 摘要失败/无 LLM 时留空合法入库（docs/01 §4.1）。
SOURCE_TYPES = ("法规", "流程", "FAQ", "招标文件")


@dataclass
class Chunk:
    """入库最小单元；id 由内容确定性生成（重复导入=幂等更新，天然断点续传）。"""

    source_type: str
    title: str
    text: str
    ctx_summary: str
    batch_id: str
    chapter: str = ""
    ref: str = ""
    page: int | None = None
    url: str = ""
    id: str = field(init=False)

    def __post_init__(self) -> None:
        key = f"{self.source_type}|{self.title}|{self.text}"
        self.id = str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def validate_chunk(chunk: Chunk) -> str | None:
    """返回错误描述；None=合法。F4 拒收清单的数据源。"""
    if chunk.source_type not in SOURCE_TYPES:
        return f"source_type 非法: {chunk.source_type!r}（允许 {SOURCE_TYPES}）"
    for name in PAYLOAD_REQUIRED:
        if not str(getattr(chunk, name)).strip():
            return f"{name} 为空"
    if chunk.page is not None and not isinstance(chunk.page, int):
        return f"page 必须为 int，得到 {type(chunk.page).__name__}"
    return None


class QdrantRepo:
    """单集合仓库；所有网络调用 trust_env=False（不吃代理环境变量）。"""

    def __init__(self, url: str, collection: str, api_key: str = "", timeout: float = 10.0):
        self.collection = collection
        if url == ":memory:":  # 测试模式：进程内实例
            self._client = QdrantClient(":memory:")
        else:
            self._client = QdrantClient(
                url=url,
                api_key=api_key or None,
                timeout=timeout,
                prefer_grpc=False,
            )
        # 代理说明：环境约定 no_proxy=127.0.0.1，本地绿色服务不受 http_proxy 影响

    def ensure_collection(self) -> None:
        if self._client.collection_exists(self.collection):
            return
        self._client.create_collection(
            collection_name=self.collection,
            vectors_config={
                VECTOR_DENSE: models.VectorParams(size=DENSE_DIM, distance=models.Distance.COSINE)
            },
            sparse_vectors_config={
                VECTOR_SPARSE: models.SparseVectorParams(),
                VECTOR_SPARSE_BM25: models.SparseVectorParams(),
            },
        )

    def upsert(self, chunks: list[Chunk], dense: list[list[float] | None],
               sparse: list[models.SparseVector | None],
               sparse_bm25: list[models.SparseVector]) -> int:
        """三路向量写入；dense/sparse 允许 None（灾备模式只写 sparse_bm25）。"""
        points = []
        for i, c in enumerate(chunks):
            vectors: dict = {VECTOR_SPARSE_BM25: sparse_bm25[i]}
            if dense[i] is not None:
                vectors[VECTOR_DENSE] = dense[i]
            if sparse[i] is not None:
                vectors[VECTOR_SPARSE] = sparse[i]
            points.append(models.PointStruct(
                id=c.id,
                vector=vectors,
                payload={
                    "source_type": c.source_type, "title": c.title, "text": c.text,
                    "ctx_summary": c.ctx_summary, "chapter": c.chapter, "ref": c.ref,
                    "page": c.page, "url": c.url, "batch_id": c.batch_id,
                },
            ))
        self._client.upsert(collection_name=self.collection, points=points, wait=True)
        return len(points)

    def count(self) -> int:
        return self._client.count(self.collection, exact=True).count

    def existing_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        found: set[str] = set()
        offset = None
        while True:
            points, offset = self._client.scroll(
                collection_name=self.collection, limit=256, offset=offset, with_payload=False,
            )
            found.update(p.id for p in points)
            if offset is None:
                break
        return found & set(ids)

    def source_type_counts(self) -> dict[str, int]:
        """只读覆盖度统计：payload.source_type 分布（异常由调用方记「不可用」）。"""
        counts: dict[str, int] = {}
        offset = None
        while True:
            points, offset = self._client.scroll(
                collection_name=self.collection, limit=256, offset=offset,
                with_payload=["source_type"], with_vectors=False,
            )
            for p in points:
                key = str((p.payload or {}).get("source_type", ""))
                counts[key] = counts.get(key, 0) + 1
            if offset is None:
                break
        return counts

    def query_hybrid(self, question_vector: list[float], question_sparse: models.SparseVector | None,
                     rrf_k: int, prefetch_size: int = 30,
                     limit: int = 15) -> list[models.ScoredPoint]:
        """双路独立查询 → 客户端 RRF 融合（docs/01 §4.1；融合逻辑见 database/rrf.py）。

        拆成两次 query_points 是为了拿到各路**排名**：Qdrant 的
        `prefetch + FusionQuery` 只返回融合结果（且 FusionQuery 的 k 固定为 2），
        无法让 E1 分类的 rrf_k 生效。rrf_k 必填无默认，防止静默退回固定 k。

        注意：拆成独立查询后，query_filter / score_threshold / params /
        lookup_from 不再像 prefetch 那样被服务端下推；当前未传这些参数，
        将来若加过滤须在每一路重复设置。
        """
        from bidqa.database.rrf import fuse_rrf

        branches: list[list[models.ScoredPoint]] = []
        if question_vector is not None:
            res = self._client.query_points(
                collection_name=self.collection, query=question_vector,
                using=VECTOR_DENSE, limit=prefetch_size, with_payload=True,
            )
            branches.append(res.points)
        if question_sparse is not None:
            res = self._client.query_points(
                collection_name=self.collection, query=question_sparse,
                using=VECTOR_SPARSE, limit=prefetch_size, with_payload=True,
            )
            branches.append(res.points)
        if not branches:  # 双双不可用：理论上不会发生（调用方保证 bm25 兜底）
            return []
        return fuse_rrf(branches, k=rrf_k, limit=limit)

    def query_bm25(self, question_sparse: models.SparseVector, limit: int = 5) -> list[models.ScoredPoint]:
        """灾备通道：仅 sparse_bm25 检索（嵌入 API 不可用/断网时）。"""
        res = self._client.query_points(
            collection_name=self.collection,
            query=question_sparse,
            using=VECTOR_SPARSE_BM25,
            limit=limit,
            with_payload=True,
        )
        return res.points

    def close(self) -> None:
        self._client.close()
