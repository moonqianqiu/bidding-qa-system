"""B2 SSE 契约测试（docs/01 §3.4：事件先后序/seq/幂等/限流/降级直答/reset→status）。

Fake 模型/检索器 monkeypatch 进 service 构建器，绝不打真实 API。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bidqa.agent.loop import LoopResult
from bidqa.api import chat_stream as cs
from bidqa.api.ratelimit import SlidingWindowLimiter
from bidqa.clients.siliconflow import DataSourceError
from bidqa.main import create_app
from bidqa.rag.retrieve import Hit, RetrievalResult


# ── fakes ──
class FakeChat:
    def __init__(self, tools_script=None, plain_script=None):
        self.tools_script = list(tools_script or [])
        self.plain_script = list(plain_script or [])

    def chat_tools(self, messages, tools, temperature=0.3):
        return self.tools_script.pop(0)

    def chat(self, messages, temperature=0.3):
        return self.plain_script.pop(0)

    def chat_stream_round(self, messages, tools, temperature=0.3):
        item = self.tools_script.pop(0)
        if item.get("tool_calls"):
            for i, tc in enumerate(item["tool_calls"]):
                fn = tc["function"]
                yield ("tool", (i, tc["id"], fn["name"], fn["arguments"]))
        else:
            yield ("content", item.get("content") or "")

    def chat_stream(self, messages, temperature=0.3):
        yield self.plain_script.pop(0)


class FakeRetriever:
    def retrieve(self, question: str, top_k: int = 5) -> RetrievalResult:
        hit = Hit(id="chunk-9", score=0.9, source_type="法规", title="招标投标法",
                  text="中标通知书对招标人和中标人具有法律效力。")
        return RetrievalResult(hits=[hit], kind="concept", rrf_k=90, mode="hybrid",
                               elapsed_ms=5)


def tool_msg(name: str, arguments: dict, call_id: str = "c1") -> dict:
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)}}]}


def text_msg(text: str) -> dict:
    return {"role": "assistant", "content": text}


@pytest.fixture()
def client(settings_factory, monkeypatch):
    settings = settings_factory()
    app = create_app(settings)
    yield TestClient(app)
    cs._client_msg_seen.clear()


def patch_service(monkeypatch, chat=None, retriever=None, pg=None, web_clients=None) -> None:
    import bidqa.api.chat_stream as mod
    monkeypatch.setattr(mod, "build_chat", lambda: chat)
    monkeypatch.setattr(mod, "build_chat_for", lambda provider: chat)
    monkeypatch.setattr(mod, "build_retriever", lambda: retriever or FakeRetriever())
    monkeypatch.setattr(mod, "build_conversation_store", lambda: pg)
    monkeypatch.setattr(mod, "build_web_clients", lambda: dict(web_clients or {}))


def stream_events(client: TestClient, payload: dict, headers: dict | None = None) -> list[dict]:
    with client.stream("POST", "/api/chat/stream", json=payload, headers=headers) as resp:
        assert resp.status_code == 200
        lines = []
        for text in resp.iter_text():
            lines.append(text)
        raw = "".join(lines)
    return [json.loads(l[5:]) for l in raw.splitlines() if l.startswith("data:")]


# ── 契约主干 ──
def test_tool_round_event_order_contract(client, monkeypatch) -> None:
    chat = FakeChat(tools_script=[
        tool_msg("search_knowledge", {"query": "中标通知书"}),
        text_msg("依据材料[1]：中标通知书发出即具法律效力。"),
    ])
    patch_service(monkeypatch, chat=chat)
    events = stream_events(client, {"message": "中标通知书效力？"})
    types = [e["type"] for e in events]
    assert types[0] == "status" and types[-1] == "done"
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)  # 单调递增
    assert types.index("token") > types.index("status")          # status*→token*→done
    done = events[-1]
    assert done["schema_v"] == 1
    assert done["tool_called"] is True and done["tool_name"] == "search_knowledge"
    assert done["tool_calls"] == [[{"name": "search_knowledge",
                                    "arguments": {"query": "中标通知书"}}]]
    assert done["rounds"] == 2
    assert done["sources"][0]["id"] == "chunk-9"
    assert done["phase_times"]["total_ms"] >= 0
    assert done["trace_id"]


def test_clarify_stream_shape(client, monkeypatch) -> None:
    """澄清轮：追问不检索不生成；done 三来源字段全空 + phase_times 第二段=澄清提问。"""
    chat = FakeChat(tools_script=[tool_msg(
        "ask_clarification", {"question_for_user": "请补充地区、时间范围与标的物"})])
    patch_service(monkeypatch, chat=chat)
    events = stream_events(client, {"message": "中标金额总共多少？"})
    done = events[-1]
    assert done["type"] == "done" and done["clarify"] is True
    assert done["sources"] == [] and done["charts"] == [] and done["web_sources"] == []
    assert done["phase_times"].get("澄清提问") == 0
    assert "向您确认" in [e.get("stage") for e in events if e["type"] == "status"]


def test_leak_reset_then_status(client, monkeypatch) -> None:
    chat = FakeChat(tools_script=[text_msg("先调用search_knowledge({'query':'x'})")],
                    plain_script=["依据材料[1]，答案是 A。"])
    patch_service(monkeypatch, chat=chat)
    events = stream_events(client, {"message": "空调采购有哪些？"})
    types = [e["type"] for e in events]
    assert "reset" in types
    assert "status" in types[types.index("reset") + 1:]  # reset 后必现 status（契约）
    assert events[-1]["type"] == "done" and "答案是 A" in "".join(
        e.get("text", "") for e in events if e["type"] == "token")


def test_degraded_direct_answer_path(client, monkeypatch) -> None:
    """Agent 基建挂掉 → 降级直答；段名 retrieve_ms/degrade_ms（两套不合并）。"""

    class BrokenChat(FakeChat):
        def chat_tools(self, messages, tools, temperature=0.3):
            raise DataSourceError("chat", "上游连接被重置")

    chat = BrokenChat(plain_script=["依据材料[1]直答。"])
    patch_service(monkeypatch, chat=chat)
    events = stream_events(client, {"message": "中标通知书效力？"})
    types = [e["type"] for e in events]
    assert "降级直答" in [e.get("stage") for e in events if e["type"] == "status"]
    done = events[-1]
    assert "retrieve_ms" in done["phase_times"] and "rounds_ms" not in done["phase_times"]
    assert any(d.startswith("agent_fallback") for d in done["degraded"])
    assert done["sources"][0]["id"] == "chunk-9"


def test_error_event_when_all_fails(client, monkeypatch) -> None:
    class DeadChat(FakeChat):
        def chat_tools(self, messages, tools, temperature=0.3):
            raise DataSourceError("chat", "全挂")

        def chat(self, messages, temperature=0.3):
            raise DataSourceError("chat", "直答也挂")

    patch_service(monkeypatch, chat=DeadChat())
    events = stream_events(client, {"message": "问题"})
    assert events[-1]["type"] == "error"
    assert events[-1]["error_code"] == 5031


def test_http_5031_when_chat_unconfigured(client, monkeypatch) -> None:
    patch_service(monkeypatch, chat=None)
    resp = client.post("/api/chat/stream", json={"message": "hi"})
    assert resp.status_code == 503
    assert resp.json()["error_code"] == 5031


# ── B4 限流 / 4091 幂等 ──
def test_rate_limit_429_with_retry_after(client, monkeypatch) -> None:
    patch_service(monkeypatch, chat=FakeChat(tools_script=[text_msg("ok")]))
    limiter = SlidingWindowLimiter(max_requests=1)
    # 端点经 rate_identity 选桶（用户体系后按身份分桶）；此处钉死单次配额桶
    monkeypatch.setattr(cs, "rate_identity", lambda request, settings: (limiter, "test"))
    first = client.post("/api/chat/stream", json={"message": "a"})
    assert first.status_code == 200
    second = client.post("/api/chat/stream", json={"message": "b"})
    assert second.status_code == 429
    assert second.json()["error_code"] == 4291
    assert "Retry-After" in second.headers


def test_idempotency_conflict_4091(client, monkeypatch) -> None:
    patch_service(monkeypatch, chat=FakeChat(tools_script=[text_msg("ok")]))
    body = {"message": "a", "client_msg_id": "msg-1"}
    with client.stream("POST", "/api/chat/stream", json=body) as r:
        assert r.status_code == 200
    resp = client.post("/api/chat/stream", json=body)
    assert resp.status_code == 409
    assert resp.json()["error_code"] == 4091
    # 换新 client_msg_id 可重试（幂等语义：不做断线续传）
    with client.stream("POST", "/api/chat/stream",
                       json={"message": "a", "client_msg_id": "msg-2"}) as r:
        assert r.status_code == 200


def test_message_length_validated(client, monkeypatch) -> None:
    resp = client.post("/api/chat/stream", json={"message": "x" * 4001})
    assert resp.status_code == 400
    assert resp.json()["error_code"] == 4001


# ── B7 组装单元 ──
def test_dedup_sources_and_charts() -> None:
    from bidqa.agent.assembly import dedup_charts, dedup_sources

    sources = [{"id": "a", "title": "A"}, {"id": "a", "title": "A"},
               {"id": "b", "title": "B"}]
    assert [s["id"] for s in dedup_sources(sources)] == ["a", "b"]

    chart = {"kind": "bar", "title": "t", "items": [{"label": "x", "value": 1}]}
    out = dedup_charts([chart, dict(chart)])
    assert len(out) == 1 and out[0]["id"]  # 内容寻址 + 去重
    other = dedup_charts([chart, {**chart, "title": "不同标题"}])
    assert len(other) == 2


def test_sanitize_chart_numeric_guards() -> None:
    from bidqa.agent.assembly import sanitize_chart

    dirty = {"kind": "bar", "title": "t", "items": [
        {"label": "a", "value": True},      # 布尔不当数字 → 剔除
        {"label": "b", "value": float("nan")},  # 非有限数 → 剔除
        {"label": "c", "value": 3.14159},   # 小数归一
    ], "max": float("inf")}
    clean = sanitize_chart(dirty)
    assert [i["label"] for i in clean["items"]] == ["c"]
    assert clean["items"][0]["value"] == 3.14
    assert clean["max"] is None
    assert sanitize_chart({"kind": "bar", "items": []}) == {}  # 无数据不出图


def test_build_done_clarify_vs_main() -> None:
    from bidqa.agent.assembly import build_done

    main = LoopResult(answer="x", tool_called=True, first_tool="search_knowledge")
    main.phase_times = {"rounds_ms": 5, "generate_ms": 0}
    done = build_done(main, elapsed_ms=9, trace_id="t")
    assert done["tool_called"] and done["phase_times"]["total_ms"] == 9

    clarify = LoopResult(answer="q", clarify=True)
    clarify.phase_times = {"rounds_ms": 3, "澄清提问": 0}
    done2 = build_done(clarify, elapsed_ms=4, trace_id="t")
    assert done2["clarify"] and done2["sources"] == [] and not done2["tool_called"]


# ── P1 英文回答：lang 契约（请求 schema 变更 → 契约测试先行）──
def test_lang_field_schema_contract() -> None:
    """缺省 zh（旧 payload 兼容）/ en 接受 / 其它值拒绝。"""
    from pydantic import ValidationError
    from bidqa.api.chat_stream import ChatStreamRequest
    assert ChatStreamRequest(message="hi").lang == "zh"
    assert ChatStreamRequest(message="hi", lang="en").lang == "en"
    with pytest.raises(ValidationError):
        ChatStreamRequest(message="hi", lang="fr")


def test_lang_en_reaches_system_prompt(client, monkeypatch) -> None:
    """lang=en 全链路：worker → AgentLoop → system prompt 语言节。"""

    class RecordingChat(FakeChat):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.round_messages: list[list[dict]] = []

        def chat_stream_round(self, messages, tools, temperature=0.3):
            self.round_messages.append(messages)
            yield from super().chat_stream_round(messages, tools, temperature)

    chat = RecordingChat(tools_script=[text_msg("Answer [1].")])
    patch_service(monkeypatch, chat=chat)
    events = stream_events(client, {"message": "空调采购项目有哪些？", "lang": "en"})
    assert events[-1]["type"] == "done"
    assert "[语言]" in chat.round_messages[0][0]["content"]

    # zh（缺省）不带语言节——旧 payload 行为不变
    chat2 = RecordingChat(tools_script=[text_msg("中文 [1]。")])
    patch_service(monkeypatch, chat=chat2)
    events2 = stream_events(client, {"message": "空调采购项目有哪些？"})
    assert events2[-1]["type"] == "done"
    assert "[语言]" not in chat2.round_messages[0][0]["content"]


# ── D4/D5 联网：注册门控 + web_enabled 挂载（供应商经 WEB_SEARCH_PROVIDERS 配置）──
def test_web_tool_called_when_enabled_and_configured(client, monkeypatch) -> None:
    """web_enabled=True + 供应商已配 → search_web 注册并被模型调用，来源进 done.sources。"""

    class FakeWeb:
        def search(self, query, depth="basic", max_results=5):
            return [{"title": "网页标题", "url": "https://ex.gov.cn/1", "content": "网页内容"}]

    chat = FakeChat(tools_script=[
        tool_msg("search_web", {"query": "安徽省政府采购公告"}),
        text_msg("依据网页[1]：公告已发布。"),
    ])
    patch_service(monkeypatch, chat=chat, web_clients={"tavily": FakeWeb()})
    events = stream_events(client, {"message": "最新的政府采购公告？", "web_enabled": True})
    done = events[-1]
    assert done["type"] == "done" and done["tool_called"] is True
    assert done["tool_name"] == "search_web"
    assert done["sources"][0]["source_type"] == "web"
    assert done["sources"][0]["url"] == "https://ex.gov.cn/1"


def test_web_enabled_without_providers_degrades_honestly(client, monkeypatch) -> None:
    """web_enabled=True 但名册留空 → 联网工具不注册，问答照常（诚实降级不断流）。"""
    chat = FakeChat(tools_script=[text_msg("依据材料[1]作答。")])
    patch_service(monkeypatch, chat=chat)  # web_clients 空
    events = stream_events(client, {"message": "空调采购有哪些？", "web_enabled": True})
    done = events[-1]
    assert done["type"] == "done"
    assert done["tool_called"] is False and done["web_sources"] == []


# ── 用户体系（docs/01 §3.1）：游客不落库+进程内历史；普通用户落库带归属 ──
class RecordingPg:
    """落库记录器：捕获 upsert 的 user_id 归属。"""

    def __init__(self):
        self.upserts: list[dict] = []

    def get_conversation(self, conv_id):
        return None

    def upsert_conversation(self, conv_id, title, messages, provider="", user_id=None):
        self.upserts.append({"conv_id": conv_id, "user_id": user_id})


def _armed_stream_client(settings_factory, monkeypatch, ident):
    """armed 应用 + 固定身份（store 替身，不打真实 PG）。"""
    from types import SimpleNamespace

    import bidqa.api.middleware as mw
    store = SimpleNamespace(resolve=lambda t: ident if t == ident.token else None,
                            guest_enabled=lambda: True)
    monkeypatch.setattr(mw, "build_users_store", lambda: store)
    return TestClient(create_app(settings_factory(USERS_ENABLED="true")))


def test_guest_stream_skips_persist_and_caches_history(settings_factory, monkeypatch):
    """游客语义：不落库（诚实）+ 进程内历史缓存（多轮上下文仍可用，重启即失）。"""
    from bidqa.auth.identity import Identity

    ident = Identity("guest", None, "guest", "tg")
    client2 = _armed_stream_client(settings_factory, monkeypatch, ident)
    pg = RecordingPg()
    patch_service(monkeypatch, chat=FakeChat(tools_script=[text_msg("答1")]), pg=pg)
    conv = "11111111-1111-1111-1111-111111111111"
    cs.guest_history.clear()
    try:
        events = stream_events(client2, {"message": "问1", "conversation_id": conv},
                               headers={"Authorization": "Bearer tg"})
        assert events[-1]["type"] == "done"
        assert pg.upserts == []  # 游客不落库
        assert cs.guest_history.load(conv) == [
            {"role": "user", "content": "问1"}, {"role": "assistant", "content": "答1"}]
        assert events[-1]["conversation_id"] == conv
    finally:
        cs.guest_history.clear()


def test_user_stream_stamps_owner(settings_factory, monkeypatch):
    """普通用户流：落库带 user_id 归属；机器/legacy（无身份）保持 NULL 机器域。"""
    from bidqa.auth.identity import Identity

    ident = Identity("user", "22222222-2222-2222-2222-222222222221", "alice", "t-fixed")
    client2 = _armed_stream_client(settings_factory, monkeypatch, ident)
    pg = RecordingPg()
    patch_service(monkeypatch, chat=FakeChat(tools_script=[text_msg("答")]), pg=pg)
    conv = "22222222-2222-2222-2222-222222222222"
    events = stream_events(client2, {"message": "问", "conversation_id": conv},
                           headers={"Authorization": "Bearer t-fixed"})
    assert events[-1]["type"] == "done"
    assert pg.upserts and pg.upserts[0]["user_id"] == ident.user_id

    # legacy（无身份）→ user_id=None（机器域，与旧行为一致）
    pg2 = RecordingPg()
    patch_service(monkeypatch, chat=FakeChat(tools_script=[text_msg("答")]), pg=pg2)
    legacy = TestClient(create_app(settings_factory()))  # CORE_ENV 默认 USERS_ENABLED=false
    stream_events(legacy, {"message": "问", "conversation_id": conv})
    assert pg2.upserts and pg2.upserts[0]["user_id"] is None
