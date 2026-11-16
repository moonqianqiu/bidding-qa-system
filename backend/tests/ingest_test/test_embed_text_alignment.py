"""审计（embed_store 编码口径）：dense/sparse 看 `ctx_summary+text`、sparse_bm25 看
`title+text`——三条通道看到不同文本，同一 chunk 在不同通道检索语义漂移。

统一为单一编码串 `encode_text(chunk)`（title + 可选 summary + text），dense/sparse 与
bm25 共用；既有集合需要重新 ingest 才与新口径一致（docs/01 §4.1）。

fake 全部内联（tests/ 无 __init__.py），:memory: 集合，不触网、不污染仓库。
"""

from __future__ import annotations

from qdrant_client import models

from bidqa.database.qdrant import Chunk, QdrantRepo
from bidqa.ingest.embed_store import encode_text, ingest_chunks
from bidqa.rag.bm25 import sparse_bm25


def _chunk(ctx_summary: str = "") -> Chunk:
    return Chunk(source_type="法规", title="招标投标法", text="正文内容",
                 ctx_summary=ctx_summary, batch_id="b1")


class _CaptureEmbedding:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed_documents(self, texts: list[str]):
        self.texts = list(texts)
        return [([1.0] + [0.0] * 1023,
                 models.SparseVector(indices=[0], values=[1.0])) for _ in texts]


def test_encode_text_includes_title_summary_text() -> None:
    assert encode_text(_chunk("摘要")) == "招标投标法\n摘要\n正文内容"
    assert encode_text(_chunk("")) == "招标投标法\n正文内容"  # 空摘要不产生空行


def test_all_three_channels_share_encoded_text(monkeypatch) -> None:
    """dense 入参与 bm25 入参必须逐字相同（同一 chunk 同一编码串）。"""
    cap = _CaptureEmbedding()
    seen: list[str] = []
    real = sparse_bm25

    def spy(text: str):
        seen.append(text)
        return real(text)

    monkeypatch.setattr("bidqa.ingest.embed_store.sparse_bm25", spy)
    repo = QdrantRepo(url=":memory:", collection="test_embed_text_v1")
    repo.ensure_collection()
    try:
        ingest_chunks(repo, [_chunk("摘要")], cap)  # type: ignore[arg-type]
    finally:
        repo.close()
    assert cap.texts == seen == ["招标投标法\n摘要\n正文内容"]
