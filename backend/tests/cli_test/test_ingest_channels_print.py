"""NIGHT-045：ingest 完成行按实际写入通道如实打印（docs/01 §4.1 口径债）。

旧文案 `dense/sparse 已写` 是口径残留——学习型 sparse 从未有值（端点实测不返回
`sparse_embedding`），改由 `IngestReport.sparse_written` 驱动三态文案：
全量三通道 / dense+sparse_bm25 两路 / 仅 sparse_bm25 灾备。
fake 全内联、:memory: 集合，零网络调用、零落盘（不污染真实仓库）。

变异自证：把 `_channels_note` 退回旧二值文案（`{'已写' if report.embedded else …}`）
→ 未返回态断言（`学习型 sparse 0/`）与全量态断言（`dense+sparse+sparse_bm25`）变红——
旧文案对两态打印同一字符串，无法区分。
"""

from __future__ import annotations

from qdrant_client import models

from bidqa.cli import main as cli

_MD = "<!-- page:1 -->\n第一章 总则\n第一条 为了规范招标投标活动。"


def _pdf_args():
    return ["ingest", "pdf", "--input", "x.pdf", "--source-type", "法规",
            "--title", "招标投标法"]


class _FakeEmbedding:
    """duck-typed EmbeddingClient：sparse_on 决定是否返回学习型稀疏，零网络。"""

    def __init__(self, sparse_on: bool):
        self.sparse_on = sparse_on

    def embed_documents(self, texts: list[str]):
        sparse = models.SparseVector(indices=[0], values=[1.0]) if self.sparse_on else None
        return [([1.0] + [0.0] * 1023, sparse) for _ in texts]


def _run(monkeypatch, embedding, capsys, collection: str) -> str:
    from bidqa.database.qdrant import QdrantRepo

    repo = QdrantRepo(url=":memory:", collection=collection)
    repo.ensure_collection()
    monkeypatch.setattr("bidqa.ingest.pdf_parse.parse_pdf",
                        lambda path, api_key=None: {"markdown": _MD, "engine": "pdfplumber"})
    monkeypatch.setattr("bidqa.database.factory.build_repo", lambda: repo)
    monkeypatch.setattr("bidqa.rag.factory.build_embedding", lambda: embedding)
    try:
        assert cli.main(_pdf_args()) == 0
    finally:
        repo.close()
    return capsys.readouterr().out


def test_channels_sparse_missing_printed_honestly(monkeypatch, capsys):
    """当前端点实测常态：dense+sparse_bm25 两路，学习型 sparse 0/N 显式可见。"""
    out = _run(monkeypatch, _FakeEmbedding(sparse_on=False), capsys, "test_chan_miss_v1")
    assert "写入通道" in out
    assert "dense+sparse_bm25" in out
    assert "学习型 sparse 0/" in out and "提供商未返回" in out
    assert "dense/sparse 已写" not in out  # 旧口径文案不得再出现


def test_channels_sparse_full_printed_honestly(monkeypatch, capsys):
    """提供商实际返回时如实报三通道全量。"""
    out = _run(monkeypatch, _FakeEmbedding(sparse_on=True), capsys, "test_chan_full_v1")
    assert "dense+sparse+sparse_bm25" in out
    assert "学习型 sparse 全量" in out


def test_channels_bm25_fallback_printed_honestly(monkeypatch, capsys):
    """嵌入 API 不可用：仅 sparse_bm25 灾备单路。"""
    out = _run(monkeypatch, None, capsys, "test_chan_fb_v1")
    assert "仅 sparse_bm25" in out and "BM25 灾备模式" in out
