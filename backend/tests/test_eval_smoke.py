"""M1-10：黄金集 smoke（引用可定位 100% 口径；local mode 全链路）。"""

import json

import pytest
from qdrant_client import models

from bidqa.database.qdrant import Chunk, QdrantRepo
from bidqa.eval.smoke import run_smoke
from bidqa.rag.bm25 import sparse_bm25
from bidqa.rag.retrieve import Retriever


@pytest.fixture
def env(tmp_path):
    repo = QdrantRepo(url=":memory:", collection="golden_v1")
    repo.ensure_collection()
    chunks = [
        Chunk(source_type="FAQ", title="中标通知书效力如何？",
              text="中标通知书对招标人和中标人具有法律效力。",
              ctx_summary="中标通知书效力如何？", batch_id="b1"),
        Chunk(source_type="FAQ", title="单一来源可以联合体吗？",
              text="一般情况下不可以。", ctx_summary="单一来源可以联合体吗？", batch_id="b1"),
    ]
    repo.upsert(chunks, [None] * 2, [None] * 2, [sparse_bm25(f"{c.title} {c.text}") for c in chunks])
    golden = tmp_path / "rag_test.jsonl"
    cases = [
        {"id": "c1", "query_type": "rag_flow", "question": "中标通知书效力如何？",
         "ref_sources": [chunks[0].id]},
        {"id": "c2", "query_type": "rag_flow", "question": "单一来源可以联合体吗？",
         "ref_sources": [chunks[1].id]},
    ]
    golden.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in cases), encoding="utf-8")
    yield repo, Retriever(repo, None, None), str(golden)
    repo.close()


def test_smoke_located_rate_100(env):
    repo, retriever, path = env
    report = run_smoke(repo, retriever, path)
    assert report["summary"]["total"] == 2
    assert report["summary"]["passed"] == 2
    assert report["summary"]["located_rate"] == 1.0


def test_smoke_detects_missing_ref(env, tmp_path):
    repo, retriever, _ = env
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"id": "x", "question": "中标通知书效力如何？",
                               "ref_sources": ["nonexistent-id"]}, ensure_ascii=False),
                   encoding="utf-8")
    report = run_smoke(repo, retriever, str(bad))
    assert report["summary"]["located_rate"] == 0.0
    assert report["summary"]["passed"] == 0


def test_smoke_latency_is_measured_not_cached(env, monkeypatch):
    """回归：p95 必须用本次调用耗时，不能读 result.elapsed_ms（缓存命中时是旧值）。"""
    repo, retriever, path = env
    real_retrieve = retriever.retrieve

    def stale_elapsed(question, top_k=5):
        result = real_retrieve(question, top_k=top_k)
        result.elapsed_ms = 99999  # 模拟缓存里首次检索的旧耗时
        return result

    monkeypatch.setattr(retriever, "retrieve", stale_elapsed)
    report = run_smoke(repo, retriever, path)
    assert all(c["elapsed_ms"] < 99999 for c in report["cases"])
    assert report["summary"]["latency_p95_ms"] < 99999
