"""M1-8：/api/ask 端点契约（成功形态 / 5031 两路 / 参数校验 4001）。"""

import pytest
from fastapi.testclient import TestClient

from bidqa.main import create_app
from bidqa.rag.retrieve import FINAL_TOP_K, RetrievalResult


class FakeRetriever:
    def __init__(self, hits=None, raise_exc=None):
        self.hits = hits if hits is not None else []
        self.raise_exc = raise_exc

    def retrieve(self, question, top_k=FINAL_TOP_K):
        if self.raise_exc:
            raise self.raise_exc
        return RetrievalResult(hits=self.hits, kind="keyword", rrf_k=30,
                               mode="bm25_fallback", elapsed_ms=1)


class FakeChat:
    def chat(self, messages, temperature=0.3):
        return "依据材料[1]：答案内容。"


def make_client(monkeypatch, retriever, chat, settings_factory):
    import bidqa.api.ask as ask_mod

    monkeypatch.setattr(ask_mod, "build_retriever", lambda: retriever)
    monkeypatch.setattr(ask_mod, "build_chat", lambda: chat)
    app = create_app(settings_factory())
    return TestClient(app)


def make_hit():
    from bidqa.rag.retrieve import Hit

    return Hit(id="abc", score=0.9, source_type="FAQ", title="问题",
               text="答案正文", batch_id="b1")


def test_ask_success_shape(settings_factory, monkeypatch):
    client = make_client(monkeypatch, FakeRetriever(hits=[make_hit()]), FakeChat(), settings_factory)
    r = client.post("/api/ask", json={"question": "测试问题"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "依据材料[1]：答案内容。"
    assert body["sources"][0]["id"] == "abc"
    assert set(body["phase_times"]) == {"retrieve_ms", "generate_ms", "total_ms"}
    assert body["mode"] == "bm25_fallback"


def test_ask_5031_when_chat_missing(settings_factory, monkeypatch):
    client = make_client(monkeypatch, FakeRetriever(hits=[make_hit()]), None, settings_factory)
    r = client.post("/api/ask", json={"question": "测试问题"})
    assert r.status_code == 503
    assert r.json()["error_code"] == 5031
    assert "chat" in r.json()["detail"]["source"]


def test_ask_5031_when_retrieval_down(settings_factory, monkeypatch):
    client = make_client(monkeypatch, FakeRetriever(raise_exc=RuntimeError("双模式全失败")), FakeChat(), settings_factory)
    r = client.post("/api/ask", json={"question": "测试问题"})
    assert r.status_code == 503
    assert "retrieval" in r.json()["detail"]["source"]


def test_ask_no_hits_honest_answer(settings_factory, monkeypatch):
    client = make_client(monkeypatch, FakeRetriever(hits=[]), FakeChat(), settings_factory)
    r = client.post("/api/ask", json={"question": "知识库没有的问题"})
    assert r.status_code == 200
    assert r.json()["answer"] == "知识库未包含相关内容。"


def test_ask_validation_4001(settings_factory, monkeypatch):
    client = make_client(monkeypatch, FakeRetriever(), FakeChat(), settings_factory)
    r = client.post("/api/ask", json={"question": ""})
    assert r.status_code == 400
    assert r.json()["error_code"] == 4001
