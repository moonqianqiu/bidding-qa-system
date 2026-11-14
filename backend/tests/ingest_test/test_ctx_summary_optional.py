"""契约：ctx_summary 改为可选（docs/01 §4.1），空摘要合法入库且不拼编码前缀。

契约测试先行。变异自证（仿 test_rrf_k_contract.py 的「旧实现变红」注记）：
① 把 ctx_summary 加回 PAYLOAD_REQUIRED → test_validate_chunk_allows_empty_ctx_summary 变红；
② 把 embed_store.py 的编码输入改回无条件拼接前缀 → test_empty_summary_chunk_* 变红。
fake 全部内联（tests/ 无 __init__.py），数据用 :memory:，不污染真实仓库、不触网。
"""

from __future__ import annotations

from qdrant_client import models

from bidqa.database.qdrant import Chunk, QdrantRepo, validate_chunk
from bidqa.ingest.embed_store import ingest_chunks


def _chunk(ctx_summary: str) -> Chunk:
    return Chunk(source_type="法规", title="招标投标法", text="正文内容",
                 ctx_summary=ctx_summary, batch_id="b1")


class _CaptureEmbedding:
    """duck-typed EmbeddingClient：只记 embed_documents 入参，不发起任何网络调用。"""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed_documents(self, texts: list[str]):
        self.texts = list(texts)
        return [([1.0] + [0.0] * 1023,
                 models.SparseVector(indices=[0], values=[1.0])) for _ in texts]


def _run_ingest(chunks: list[Chunk]):
    cap = _CaptureEmbedding()
    repo = QdrantRepo(url=":memory:", collection="test_ctx_summary_v1")
    repo.ensure_collection()
    try:
        report = ingest_chunks(repo, chunks, cap)  # type: ignore[arg-type]
    finally:
        repo.close()
    return cap.texts, report


def test_validate_chunk_allows_empty_ctx_summary():
    assert validate_chunk(_chunk("")) is None
    assert validate_chunk(_chunk("上下文摘要")) is None


def test_empty_summary_chunk_is_ingested_not_rejected():
    texts, report = _run_ingest([_chunk("")])
    assert report.rejected == []
    assert report.upserted == 1
    assert texts == ["正文内容"]  # 空摘要不拼前缀、不带前导换行


def test_nonempty_summary_keeps_faq_encoding_input_unchanged():
    texts, _ = _run_ingest([_chunk("上下文摘要")])
    assert texts == ["上下文摘要\n正文内容"]  # FAQ 路径逐字不变
