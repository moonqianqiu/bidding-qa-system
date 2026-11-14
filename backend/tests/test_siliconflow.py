"""M1-2：嵌入/重排客户端（respx 模拟；查询与文档同一入口锁定）。"""

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
def test_retry_on_429_then_success():
    route = respx.post(f"{BASE}/embeddings").mock(side_effect=[
        httpx.Response(429, json={}),
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
def test_rerank_happy_path():
    respx.post(f"{BASE}/rerank").mock(return_value=httpx.Response(200, json={"results": [
        {"index": 2, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.4}]}))
    client = RerankClient(BASE, "sk-test", "BAAI/bge-reranker-v2-m3")
    ranked = client.rerank("q", ["d0", "d1", "d2"], top_n=2)
    assert ranked == [(2, 0.9), (0, 0.4)]
