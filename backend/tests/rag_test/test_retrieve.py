"""M1-5/E1-E5：混合召回（BM25 灾备模式）+ 有界 FIFO 缓存深拷贝语义。"""

import pytest
from qdrant_client import models

from bidqa.database.qdrant import Chunk, QdrantRepo
from bidqa.rag.bm25 import sparse_bm25
from bidqa.rag.retrieve import Retriever


@pytest.fixture
def retriever():
    repo = QdrantRepo(url=":memory:", collection="test_v1")
    repo.ensure_collection()
    chunks = [
        Chunk(source_type="FAQ", title="单一来源可以联合体吗？",
              text="一般情况下不可以。但如果标的必须联合体才能提供，可以作为供应商。",
              ctx_summary="单一来源可以联合体吗？", batch_id="b1"),
        Chunk(source_type="FAQ", title="中标通知书效力如何？",
              text="中标通知书对招标人和中标人具有法律效力。",
              ctx_summary="中标通知书效力如何？", batch_id="b1"),
        Chunk(source_type="FAQ", title="传感器有哪些品牌？",
              text="中国电科等。", ctx_summary="传感器有哪些品牌？", batch_id="b1"),
    ]
    repo.upsert(chunks, [None] * 3, [None] * 3, [sparse_bm25(f"{c.title} {c.text}") for c in chunks])
    yield Retriever(repo, embedding=None, reranker=None)
    repo.close()


def test_bm25_fallback_mode_retrieves(retriever):
    result = retriever.retrieve("单一来源可以联合体吗？")
    assert result.mode == "bm25_fallback"
    assert result.hits and result.hits[0].title == "单一来源可以联合体吗？"
    assert result.degraded == []
    assert result.elapsed_ms >= 0


def test_hit_as_source_shape(retriever):
    result = retriever.retrieve("中标通知书效力如何？")
    src = result.hits[0].as_source()
    assert set(src) == {"id", "source_type", "title", "text", "chapter",
                        "ref", "page", "url", "score"}


def test_cache_returns_deepcopy(retriever):
    r1 = retriever.retrieve("中标通知书效力如何？")
    r2 = retriever.retrieve("中标通知书效力如何？")
    assert r1 is not r2 and r1.hits[0] is not r2.hits[0]  # 深拷贝，防缓存污染
    assert r1.hits[0].score == r2.hits[0].score


# ── E3：两路独立召回 + 客户端 RRF（需要真实 dense/sparse 向量）──

class FakeEmbedding:
    def embed_query(self, text: str):
        return [1.0] + [0.0] * 1023, models.SparseVector(indices=[1], values=[1.0])


class FakeReranker:
    def __init__(self, ranked):
        self.ranked = ranked

    def rerank(self, query, documents, top_n):
        return self.ranked


@pytest.fixture
def hybrid_repo():
    repo = QdrantRepo(url=":memory:", collection="test_hybrid_v1")
    repo.ensure_collection()
    chunks = [Chunk(source_type="FAQ", title=f"问题{i}", text=f"答案内容{i}",
                    ctx_summary=f"问题{i}", batch_id="b1") for i in range(3)]
    dense = [[1.0] + [0.0] * 1023, [0.0, 1.0] + [0.0] * 1022,
             [0.0, 0.0, 1.0] + [0.0] * 1021]
    sparse = [models.SparseVector(indices=[1, 2], values=[1.0, 0.5]),
              models.SparseVector(indices=[1], values=[0.9]),
              models.SparseVector(indices=[2], values=[0.8])]
    repo.upsert(chunks, dense, sparse,
                [sparse_bm25(f"{c.title} {c.text}") for c in chunks])
    yield repo
    repo.close()


def test_hybrid_without_rerank_score_is_none(hybrid_repo):
    result = Retriever(hybrid_repo, FakeEmbedding(), reranker=None).retrieve("问题0")
    assert result.mode == "hybrid" and result.hits
    assert all(h.score is None for h in result.hits)  # RRF 排名分不冒充相关度
    assert result.hits[0].as_source()["score"] is None
    assert result.rrf_k > 0


def test_hybrid_with_rerank_score_is_relevance(hybrid_repo):
    result = Retriever(hybrid_repo, FakeEmbedding(),
                       FakeReranker([(0, 0.8), (1, 0.2)])).retrieve("问题0")
    assert result.hits[0].score == 0.8
    assert result.hits[1].score == 0.2


def test_bm25_fallback_score_is_none(retriever):
    result = retriever.retrieve("单一来源可以联合体吗？")
    assert all(h.score is None for h in result.hits)


def test_cache_deepcopy_with_none_score(hybrid_repo):
    r = Retriever(hybrid_repo, FakeEmbedding(), reranker=None)
    r1 = r.retrieve("问题0")
    r2 = r.retrieve("问题0")
    assert r1 is not r2 and r1.hits[0] is not r2.hits[0]
    assert r1.hits[0].score is None and r2.hits[0].score is None


def test_crag_mutation_does_not_poison_cache(retriever):
    """P1-10（审计）：put 曾存返回给调用方的同一对象——CRAG 就地 append degraded
    会把降级注记写进缓存（复证：第二次检索带旧注记）。"""
    r1 = retriever.retrieve("中标通知书效力如何？")
    r1.degraded.append("crag_low_confidence(0.42<0.5): 无改写模型")  # 模拟 crag 就地改
    r2 = retriever.retrieve("中标通知书效力如何？")
    assert r2.degraded == []  # 缓存对象独立于调用方持有的引用


def test_degraded_result_not_cached(retriever):
    """P1-10（硬约束 2 只缓存成功结果）：降级结果不入缓存——embedding 故障恢复后
    同问题必须重新真检索，绝不返回旧故障形态（bm25_fallback）。"""
    from bidqa.clients.siliconflow import DataSourceError

    calls = {"n": 0}

    class FlakyEmbedding:
        def embed_query(self, text: str):
            calls["n"] += 1
            if calls["n"] == 1:
                raise DataSourceError("embedding", "模拟故障")
            return [1.0] + [0.0] * 1023, models.SparseVector(indices=[1], values=[1.0])

    retriever.embedding = FlakyEmbedding()
    r1 = retriever.retrieve("中标通知书效力如何？")
    assert r1.mode == "bm25_fallback" and r1.degraded  # 首次降级
    r2 = retriever.retrieve("中标通知书效力如何？")
    assert calls["n"] == 2  # 恢复后重新真检索（若降级结果被缓存则不再调用=红）
    assert r2.mode == "hybrid" and not r2.degraded
