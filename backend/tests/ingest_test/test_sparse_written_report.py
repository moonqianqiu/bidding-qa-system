"""NIGHT-045：IngestReport 携带学习型 sparse 实写条数（docs/01 §4.1 口径债）。

sparse_written = 实际写入 `sparse` 通道的条数（upsert 对 None 跳通道，
database/qdrant.py——计数与落库一致）；0=提供商未返回 sparse_embedding，
不是「得零分」式的错误值。fake 全内联、:memory: 集合，零网络调用。

变异自证：把 sparse_written 计算改回恒 0 → 全量态断言变红；
改回恒 len(texts) → 灾备/未返回态断言变红。
"""

from __future__ import annotations

from qdrant_client import models

from bidqa.database.qdrant import Chunk, QdrantRepo
from bidqa.ingest.embed_store import IngestReport, ingest_chunks


def _chunk() -> Chunk:
    return Chunk(source_type="法规", title="招标投标法", text="正文内容",
                 ctx_summary="", batch_id="b1")


class _FakeEmbedding:
    """duck-typed EmbeddingClient：sparse_on 决定是否返回学习型稀疏，零网络。"""

    def __init__(self, sparse_on: bool):
        self.sparse_on = sparse_on

    def embed_documents(self, texts: list[str]):
        sparse = models.SparseVector(indices=[0], values=[1.0]) if self.sparse_on else None
        return [([1.0] + [0.0] * 1023, sparse) for _ in texts]


def _run(embedding) -> IngestReport:
    repo = QdrantRepo(url=":memory:", collection="test_sparse_report_v1")
    repo.ensure_collection()
    try:
        return ingest_chunks(repo, [_chunk()], embedding)
    finally:
        repo.close()


def test_report_counts_sparse_when_provider_returns():
    report = _run(_FakeEmbedding(sparse_on=True))
    assert report.embedded and report.upserted == 1
    assert report.sparse_written == 1  # 全量写入


def test_report_zero_when_provider_skips_sparse():
    report = _run(_FakeEmbedding(sparse_on=False))
    assert report.embedded and report.upserted == 1  # dense 照常写入
    assert report.sparse_written == 0  # 学习型 sparse 通道跳过（当前端点实测常态）


def test_report_zero_in_bm25_fallback():
    report = _run(None)  # 嵌入 API 不可用：仅 sparse_bm25
    assert not report.embedded and report.sparse_written == 0
