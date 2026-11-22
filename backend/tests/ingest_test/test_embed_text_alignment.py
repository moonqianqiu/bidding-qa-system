"""审计（embed_store 编码口径）：dense/sparse 看 `ctx_summary+text`、sparse_bm25 看
`title+text`——三条通道看到不同文本，同一 chunk 在不同通道检索语义漂移。

统一为单一编码串 `encode_text(chunk)`（title + 可选 summary + text），dense/sparse 与
bm25 共用；既有集合需要重新 ingest 才与新口径一致（docs/01 §4.1）。

NIGHT-042（方案 B1）：sparse_bm25 文档侧烘焙 BM25 权重（tf 饱和+长度归一），
avgdl=本批平均词元数（全量 ingest=集合均值）；查询侧只发原始词频。

fake 全部内联（tests/ 无 __init__.py），:memory: 集合，不触网、不污染仓库。
"""

from __future__ import annotations

from typing import cast

import pytest
from qdrant_client import models

from bidqa.database.qdrant import Chunk, QdrantRepo
from bidqa.ingest.embed_store import encode_text, ingest_chunks
from bidqa.rag.bm25 import sparse_bm25_doc, tokenize


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
    avgdls: list[float] = []
    real = sparse_bm25_doc

    def spy(text: str, avgdl: float):
        seen.append(text)
        avgdls.append(avgdl)
        return real(text, avgdl)

    monkeypatch.setattr("bidqa.ingest.embed_store.sparse_bm25_doc", spy)
    repo = QdrantRepo(url=":memory:", collection="test_embed_text_v1")
    repo.ensure_collection()
    try:
        ingest_chunks(repo, [_chunk("摘要")], cap)  # type: ignore[arg-type]
    finally:
        repo.close()
    assert cap.texts == seen == ["招标投标法\n摘要\n正文内容"]
    assert avgdls == [len(tokenize("招标投标法\n摘要\n正文内容"))]  # 本批均值（单块=自身 dl）


def test_ingest_bakes_bm25_doc_weights_into_collection() -> None:
    """入库断言：sparse_bm25 通道落库值=烘焙 BM25 权重（非原始词频）。

    tf=2 的重复词元在 dl=avgdl 时权重 2·2.5/(2+1.5)≈1.43 < 2（饱和）——
    若通道仍写词频该断言必红（变异可红）。
    """
    chunk = Chunk(source_type="FAQ", title="保证金",
                  text="保证金 估算 保证金", ctx_summary="", batch_id="b1")
    repo = QdrantRepo(url=":memory:", collection="test_bake_v1")
    repo.ensure_collection()
    try:
        ingest_chunks(repo, [chunk], None)  # 灾备形态：仅 sparse_bm25
        pid, payload, vectors = next(iter(repo.iter_points(with_vectors=True)))
        assert str(pid) == chunk.id and vectors is not None
        vec = cast(dict, vectors)["sparse_bm25"]  # qdrant-client vector 联合类型，测试收窄
        expected = sparse_bm25_doc(encode_text(chunk),
                                   avgdl=len(tokenize(encode_text(chunk))))
        assert vec.indices == expected.indices
        assert vec.values == pytest.approx(expected.values)
        assert max(vec.values) < 2.0  # tf=2 已饱和，不可能是原始词频
    finally:
        repo.close()
