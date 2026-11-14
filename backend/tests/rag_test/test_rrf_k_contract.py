"""E1/E3 契约：classify() 的 rrf_k 必须真正进入 RRF 融合（docs/01 §4.1）。

契约测试先行：本文件先在旧实现（FusionQuery 固定 k=2、rrf_k 零消费）上变红，
实现后转绿。fake 全部内联（tests/ 无 __init__.py），数据用 :memory:/纯内存对象，
不污染真实仓库。
"""

from __future__ import annotations

import inspect

import pytest
from qdrant_client import models

from bidqa.database.qdrant import QdrantRepo, VECTOR_DENSE, VECTOR_SPARSE
from bidqa.rag.classify import classify
from bidqa.rag.retrieve import Retriever


def sp(pid: str, score: float) -> models.ScoredPoint:
    return models.ScoredPoint(id=pid, version=0, score=score,
                              payload={"title": f"T{pid}", "text": f"x{pid}"})


class FakeEmbedding:
    def embed_query(self, text: str):
        return [1.0] + [0.0] * 1023, models.SparseVector(indices=[0], values=[1.0])


class FakeRepo:
    """捕获 query_hybrid 收到的 rrf_k；返回两路可转 Hit 的点。"""

    def __init__(self):
        self.k_values: list = []

    def query_hybrid(self, q_dense, q_sparse, *args, **kwargs):
        self.k_values.append(kwargs.get("rrf_k"))
        return [sp("a", 0.9), sp("b", 0.8)]

    def query_bm25(self, *args, **kwargs):
        return []


# 三句分别命中 keyword / concept / mixed（词表 rag/keywords.yaml）
QUESTIONS = (
    "安徽省最近有哪些安装类项目中标",
    "供应商提出的异议应当在几日内答复",
    "中标项目的异议应当怎么处理",
)


def test_query_hybrid_requires_rrf_k():
    """rrf_k 必填无默认，防止以后再静默退回固定 k。"""
    params = inspect.signature(QdrantRepo.query_hybrid).parameters
    assert "rrf_k" in params, "query_hybrid 必须接受 rrf_k"
    assert params["rrf_k"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        QdrantRepo.query_hybrid(None, [1.0], None)  # 漏传 k


def test_classify_k_reaches_query_hybrid():
    """三分类的 k 必须一路传到 query_hybrid，且与 result.rrf_k 一致。"""
    repo = FakeRepo()
    retriever = Retriever(repo, FakeEmbedding(), reranker=None)
    k_values = []
    for q in QUESTIONS:
        kind, k = classify(q)
        result = retriever.retrieve(q)
        assert repo.k_values[-1] == k, f"{kind} 的 k 未传给 query_hybrid"
        assert result.rrf_k == k
        k_values.append(k)
    assert len(set(k_values)) == 3, "三句应覆盖三个不同的 k"


def test_fuse_rrf_k_changes_ranking():
    """同一批两路结果，k=2 与 k=90 的融合分/排序必须不同。"""
    from bidqa.database.rrf import fuse_rrf

    dense = [sp("a", 0.9), sp("b", 0.8), sp("c", 0.7)]
    sparse = [sp("b", 5.0), sp("a", 4.0)]
    low = fuse_rrf([dense, sparse], k=2, limit=3)
    high = fuse_rrf([dense, sparse], k=90, limit=3)
    assert [p.id for p in low] == [p.id for p in high] or low[0].score != high[0].score
    assert abs(low[0].score - high[0].score) > 1e-6


def test_fuse_rrf_dedup_and_no_mutation():
    """同一 id 跨路只累加一次贡献；输入 score 不被就地改写。"""
    from bidqa.database.rrf import fuse_rrf

    dense = [sp("a", 0.9), sp("b", 0.8)]
    sparse = [sp("a", 5.0)]
    before = (dense[0].score, sparse[0].score)
    out = fuse_rrf([dense, sparse], k=60, limit=2)
    assert (dense[0].score, sparse[0].score) == before, "不得就地改输入 score"
    assert out[0] is not dense[0] and out[0] is not sparse[0], "必须返回新对象"
    # a 在两路都出现：rank1+rank1 = 2/(k+1)；b 只在 dense rank2 = 1/(k+2)
    assert out[0].id == "a"
    assert abs(out[0].score - 2 / 61) < 1e-9
    assert abs(out[1].score - 1 / 62) < 1e-9


def test_fuse_rrf_uneven_branches():
    """稀疏路远短于稠密路时，只对出现的 rank 累加；空路返回 []。"""
    from bidqa.database.rrf import fuse_rrf

    dense = [sp("a", 0.9), sp("b", 0.8), sp("c", 0.7)]
    sparse = [sp("c", 5.0)]
    out = fuse_rrf([dense, sparse], k=30, limit=3)
    assert {p.id for p in out} == {"a", "b", "c"}
    assert fuse_rrf([[], []], k=30, limit=3) == []
    assert fuse_rrf([dense], k=30, limit=2)[0].id == "a"


def test_hybrid_does_not_use_fusion_query(monkeypatch):
    """两路独立 query_points，不再用 prefetch + FusionQuery/RrfQuery。"""
    repo = QdrantRepo(url=":memory:", collection="contract_v1")
    repo.ensure_collection()
    captured: list[dict] = []
    orig = repo._client.query_points

    def spy(**kwargs):
        captured.append(kwargs)
        return orig(**kwargs)

    monkeypatch.setattr(repo._client, "query_points", spy)
    try:
        repo.query_hybrid([1.0] + [0.0] * 1023,
                          models.SparseVector(indices=[0], values=[1.0]),
                          rrf_k=90, prefetch_size=30, limit=15)
    finally:
        repo.close()
    assert len(captured) == 2, "应拆成两次独立 query_points"
    assert {c.get("using") for c in captured} == {VECTOR_DENSE, VECTOR_SPARSE}
    assert not any(isinstance(c.get("query"), (models.FusionQuery, models.RrfQuery))
                   for c in captured), "不得再用服务端融合查询对象"
    assert all("prefetch" not in c for c in captured)
