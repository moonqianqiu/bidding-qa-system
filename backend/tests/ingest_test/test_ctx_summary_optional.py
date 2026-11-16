"""契约：ctx_summary 改为可选（docs/01 §4.1），空摘要合法入库且不产生空行。

编码口径已统一（审计 embed_store 债）：三条向量通道共用 `encode_text` =
title + 可选 summary + text；本文件的入参断言随口径更新（旧值 `text` /
`summary\\ntext`，见 test_embed_text_alignment.py 的一致性断言）。

契约测试先行。变异自证（仿 test_rrf_k_contract.py 的「旧实现变红」注记）：
① 把 ctx_summary 加回 PAYLOAD_REQUIRED → test_validate_chunk_allows_empty_ctx_summary 变红；
② 把 encode_text 改回无条件拼接前缀 → test_empty_summary_chunk_is_ingested_not_rejected 变红。
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
    assert texts == ["招标投标法\n正文内容"]  # 空摘要跳过、无空行；title 参与编码（统一口径）


def test_nonempty_summary_encoding_includes_title_and_summary():
    texts, _ = _run_ingest([_chunk("上下文摘要")])
    assert texts == ["招标投标法\n上下文摘要\n正文内容"]  # title→summary→text 三段
