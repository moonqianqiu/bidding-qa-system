"""M1-2：嵌入/重排客户端（respx 模拟；查询与文档同一入口锁定）。"""

import logging

import httpx
import pytest
import respx

from bidqa.clients.siliconflow import DataSourceError, EmbeddingClient, RerankClient

BASE = "https://api.siliconflow.example/v1"


def make_client() -> EmbeddingClient:
    return EmbeddingClient(BASE, "sk-test", batch_size=2)


@respx.mock
def test_embed_documents_dense_and_sparse():
    route = respx.post(f"{BASE}/embeddings").mock(return_value=httpx.Response(200, json={"data": [
        {"embedding": [0.1] * 4, "sparse_embedding": {"indices": [3, 7], "values": [0.5, 0.25]}},
        {"embedding": [0.2] * 4},
    ]}))
    client = make_client()
    out = client.embed_documents(["a", "b"])
    assert route.called
    body = route.calls.last.request.read().decode()
    assert '"input"' in body and "BAAI/bge-m3" in body
    assert out[0][0] == [0.1] * 4
    assert out[0][1].indices == [3, 7]
    assert out[1][1] is None  # 无 sparse_embedding → None（降级语义）


@respx.mock
def test_embed_query_uses_same_entry():
    route = respx.post(f"{BASE}/embeddings").mock(return_value=httpx.Response(200, json={"data": [
        {"embedding": [0.3] * 4}]}))
    client = make_client()
    dense, sparse = client.embed_query("单一来源采购")
    assert dense == [0.3] * 4 and sparse is None
    # 同一入口：查询与文档请求体结构一致（模型/endpoint）
    assert "/embeddings" in str(route.calls.last.request.url)


@respx.mock
def test_429_raises_immediately_no_retry():
    """429 红线（AGENTS/docs/01 §5）：嵌入/重排等非 sensenova 端点绝不重试 429，
    立即 DataSourceError——原实现默认 retry_429=True 曾违反定版红线（审计 P0-3）。"""
    route = respx.post(f"{BASE}/embeddings").mock(
        return_value=httpx.Response(429, json={}))
    client = make_client()
    with pytest.raises(DataSourceError, match="429"):
        client.embed_documents(["x"])
    assert route.call_count == 1  # 一次都不重试


@respx.mock
def test_5xx_retry_then_success():
    """5xx/网络错误退避重试保留（与 429 定向重试的边界：只有 429 按端点白名单）。"""
    route = respx.post(f"{BASE}/embeddings").mock(side_effect=[
        httpx.Response(500, json={}),
        httpx.Response(200, json={"data": [{"embedding": [0.1] * 4}]}),
    ])
    client = make_client()
    out = client.embed_documents(["x"])
    assert route.call_count == 2 and out[0][0] == [0.1] * 4


@respx.mock
def test_500_exhausted_raises():
    respx.post(f"{BASE}/embeddings").mock(return_value=httpx.Response(500, json={"e": 1}))
    client = make_client()
    with pytest.raises(DataSourceError):
        client.embed_documents(["x"])


@respx.mock
def test_sparse_zero_ratio_warns_once(caplog):
    """NIGHT-045 可观测性：本批 sparse item 占比 0 → 单入口（_embed）WARNING
    进程级只打一次；不拒收、不改返回形态（dense 照常返回、sparse=None）。
    变异自证：删 warning 块 → 0 条记录变红；去掉 _sparse_zero_warned 门 →
    多次调用出多条记录变红。"""
    respx.post(f"{BASE}/embeddings").mock(return_value=httpx.Response(200, json={"data": [
        {"embedding": [0.1] * 4}, {"embedding": [0.2] * 4}]}))
    EmbeddingClient._sparse_zero_warned = False
    client = make_client()
    with caplog.at_level(logging.WARNING, logger="bidqa.clients.siliconflow"):
        out1 = client.embed_documents(["a", "b"])
        out2 = client.embed_documents(["c", "d"])
        client.embed_query("q")  # 查询同走 _embed 单入口——不得再打
    assert all(d[0] == [0.1] * 4 or d[0] == [0.2] * 4 for d in out1)  # dense 不受影响
    assert all(d[1] is None for d in (*out1, *out2))  # 数据形态不变、不拒收
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1  # 单入口只打一次
    assert "0/2" in warns[0].getMessage() and "sparse" in warns[0].getMessage()


@respx.mock
def test_sparse_present_no_warning(caplog):
    """提供商实际返回 sparse → 无告警（观测语义：只对空置打 WARNING）。"""
    respx.post(f"{BASE}/embeddings").mock(return_value=httpx.Response(200, json={"data": [
        {"embedding": [0.1] * 4, "sparse_embedding": {"indices": [3], "values": [0.5]}}]}))
    EmbeddingClient._sparse_zero_warned = False
    client = make_client()
    with caplog.at_level(logging.WARNING, logger="bidqa.clients.siliconflow"):
        out = client.embed_documents(["a"])
    assert out[0][1] is not None and out[0][1].indices == [3]
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


@respx.mock
def test_rerank_happy_path():
    respx.post(f"{BASE}/rerank").mock(return_value=httpx.Response(200, json={"results": [
        {"index": 2, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.4}]}))
    client = RerankClient(BASE, "sk-test", "BAAI/bge-reranker-v2-m3")
    ranked = client.rerank("q", ["d0", "d1", "d2"], top_n=2)
    assert ranked == [(2, 0.9), (0, 0.4)]
