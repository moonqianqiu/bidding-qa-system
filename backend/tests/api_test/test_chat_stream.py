"""B2 SSE 契约测试（docs/01 §3.4：事件先后序/seq/幂等/限流/降级直答/reset→status）。

Fake 模型/检索器 monkeypatch 进 service 构建器，绝不打真实 API。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bidqa.agent.loop import LoopResult
from bidqa.api.chat import guards as guards_mod
from bidqa.api.chat import stream as stream_mod
from bidqa.application.chat import availability as availability_mod
from bidqa.application.chat import degrade as degrade_mod
from bidqa.application.chat import history as history_mod
from bidqa.application.chat import online as app_online_mod
from bidqa.application.chat import worker as worker_mod
from bidqa.api.ratelimit import SlidingWindowLimiter
from bidqa.clients.siliconflow import DataSourceError
from bidqa.config import Settings
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
    guards_mod.clear_seen()


def patch_service(monkeypatch, chat=None, retriever=None, pg=None, web_clients=None) -> None:
    """patch 缝随 C3 拆分移到 application 层；bp/neo/reranker 钉 None 保测试密闭。"""
    monkeypatch.setattr(worker_mod, "build_chat_for", lambda provider: chat)
    monkeypatch.setattr(worker_mod, "build_retriever", lambda: retriever or FakeRetriever())
    monkeypatch.setattr(worker_mod, "build_conversation_store", lambda: pg)
    monkeypatch.setattr(worker_mod, "build_web_clients", lambda: dict(web_clients or {}))
    monkeypatch.setattr(worker_mod, "build_neo", lambda: None)
    monkeypatch.setattr(worker_mod, "build_bp_repo", lambda: None)
    monkeypatch.setattr(worker_mod, "build_reranker", lambda: None)
    monkeypatch.setattr(degrade_mod, "build_chat_for", lambda provider: chat)
    monkeypatch.setattr(degrade_mod, "build_retriever", lambda: retriever or FakeRetriever())
    monkeypatch.setattr(availability_mod, "build_chat_for",
                        lambda provider: chat)  # 5031 前置门（与 worker 同一判据）


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
    assert done["schema_v"] == 2  # WP5b：+plan_steps/reflected（docs/01 §3.4）
    assert done["plan_steps"] == [] and done["reflected"] is False  # 恒在位，空不省略
    assert done["tool_called"] is True and done["tool_name"] == "search_knowledge"
    assert done["tool_calls"] == [[{"name": "search_knowledge",
                                    "arguments": {"query": "中标通知书"}}]]
    assert done["rounds"] == 2
    assert done["sources"][0]["id"] == "chunk-9"
    assert done["phase_times"]["total_ms"] >= 0
    assert done["trace_id"]


# ── WP5b：done 轨迹字段 plan_steps/reflected（schema_v=2，docs/01 §3.4）──
def _flagged_client(settings_factory, **flags):
    return TestClient(create_app(settings_factory(**flags)))


def test_plan_stream_contract(settings_factory, monkeypatch):
    """规划路径：规划调用前 status「规划中」；done.plan_steps 透传计划、reflected=false、
    schema_v=2（reflect 关避免消费 plain_script，口径聚焦 plan）。"""
    chat = FakeChat(
        tools_script=[
            tool_msg("search_knowledge", {"query": "空调中标金额"}),
            text_msg("依据材料[1]：2023年X万元、2024年Y万元。"),
        ],
        plain_script=['{"steps": ["查2023年空调中标金额", "查2024年空调中标金额"]}'],
    )
    patch_service(monkeypatch, chat=chat)
    client2 = _flagged_client(settings_factory, AGENT_REFLECT_ENABLED="false")
    events = stream_events(client2, {"message": "分别对比2023年和2024年的空调中标金额"})
    done = events[-1]
    assert done["type"] == "done" and done["schema_v"] == 2
    assert done["plan_steps"] == ["查2023年空调中标金额", "查2024年空调中标金额"]
    assert done["reflected"] is False
    stages = [e.get("stage") for e in events if e["type"] == "status"]
    assert "规划中" in stages  # 仅实际发起规划调用前出现
    assert "分析中" in stages and "取数中" in stages  # 规划后照常进入工具循环


def test_reflect_stream_contract(settings_factory, monkeypatch):
    """自检路径：自检调用前 status「自检中」；need_more=true → 补查轮 + done.reflected=true；
    plan 关闭时 plan_steps=[]（恒在位）。"""
    chat = FakeChat(
        tools_script=[
            tool_msg("search_knowledge", {"query": "中标通知书"}),
            text_msg("补充2024年数据后作答。"),
        ],
        plain_script=['{"need_more": true, "hint": "缺2024年数据"}'],
    )
    patch_service(monkeypatch, chat=chat)
    client2 = _flagged_client(settings_factory, AGENT_PLAN_ENABLED="false")
    events = stream_events(client2, {"message": "中标通知书效力？"})
    done = events[-1]
    assert done["type"] == "done" and done["schema_v"] == 2
    assert done["reflected"] is True  # 自检判定证据不足并触发补查
    assert done["plan_steps"] == []   # 未规划：字段在位且为空
    stages = [e.get("stage") for e in events if e["type"] == "status"]
    assert "自检中" in stages         # 仅实际发起自检调用前出现
    assert stages.index("自检中") < stages.index("分析中", stages.index("自检中"))  # 补查轮发生


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


# ── 命名档前置门（2026-11-14 审计缺陷：只配命名档被主槽位判据误 5031）──
def _profile_only_client(monkeypatch, chat: FakeChat | None) -> TestClient:
    """只配命名档、主槽位留空的部署；availability/worker/degrade 三缝同路由。"""
    monkeypatch.setenv("CHAT_FAST_BASE_URL", "http://profile.local")
    monkeypatch.setenv("CHAT_FAST_API_KEY", "k-fast")
    monkeypatch.setenv("CHAT_FAST_MODEL", "fast-model")
    settings = Settings(_env_file=None, QDRANT_URL="http://q", USERS_ENABLED="false")

    def route(provider: str | None):
        return chat if provider == "fast" else None  # 未知/缺省 → 主槽位（不存在）

    monkeypatch.setattr(availability_mod, "build_chat_for", route)
    monkeypatch.setattr(worker_mod, "build_chat_for", route)
    monkeypatch.setattr(degrade_mod, "build_chat_for", route)
    monkeypatch.setattr(worker_mod, "build_retriever", lambda: FakeRetriever())
    monkeypatch.setattr(worker_mod, "build_conversation_store", lambda: None)
    monkeypatch.setattr(worker_mod, "build_web_clients", lambda: {})
    monkeypatch.setattr(worker_mod, "build_neo", lambda: None)
    monkeypatch.setattr(worker_mod, "build_bp_repo", lambda: None)
    monkeypatch.setattr(worker_mod, "build_reranker", lambda: None)
    return TestClient(create_app(settings))


def test_profile_only_provider_passes_precheck(monkeypatch) -> None:
    client = _profile_only_client(monkeypatch, FakeChat(tools_script=[text_msg("依据材料[1]直答。")]))
    events = stream_events(client, {"message": "问题", "provider": "fast"})
    assert events[-1]["type"] == "done"
    assert events[-1]["tool_name"] == ""  # 纯直答轮


def test_profile_only_unknown_provider_falls_back_to_absent_main(monkeypatch) -> None:
    client = _profile_only_client(monkeypatch, FakeChat(plain_script=["x"]))
    resp = client.post("/api/chat/stream", json={"message": "hi", "provider": "nope"})
    assert resp.status_code == 503 and resp.json()["error_code"] == 5031


def test_profile_only_no_provider_uses_absent_main(monkeypatch) -> None:
    client = _profile_only_client(monkeypatch, FakeChat(plain_script=["x"]))
    resp = client.post("/api/chat/stream", json={"message": "hi"})
    assert resp.status_code == 503 and resp.json()["error_code"] == 5031


def test_degrade_uses_provider_profile(monkeypatch) -> None:
    """降级直答与主路径同源 build_chat_for：命名档部署主路径失败后仍可降级。"""
    called: list[str | None] = []
    chat = FakeChat(plain_script=["依据材料[1]直答。"])
    monkeypatch.setattr(degrade_mod, "build_chat_for",
                        lambda provider: (called.append(provider), chat)[1])
    monkeypatch.setattr(degrade_mod, "build_retriever", lambda: FakeRetriever())
    out = degrade_mod.direct_rag_answer(
        "问题", "zh", [], lambda stage, data: None, "boom", provider="fast")
    assert called == ["fast"]
    assert out.answer == "依据材料[1]直答。"


# ── G4 在线评测埋点（v2.2 下沉 application/chat/online；settings 经 DI 注入）──
def test_online_record_called_on_done(settings_factory, monkeypatch) -> None:
    settings = settings_factory(ONLINE_EVAL_ENABLED="true",
                                ONLINE_EVAL_SAMPLE_RATE="1.0")
    client = TestClient(create_app(settings))
    chat = FakeChat(tools_script=[text_msg("依据材料[1]直答。")])
    patch_service(monkeypatch, chat=chat)
    recorded: dict = {}

    class FakeOnline:
        def record_done(self, **kw):
            recorded.update(kw)

    monkeypatch.setattr(app_online_mod, "get_online", lambda: FakeOnline())
    events = stream_events(client, {"message": "中标通知书效力？"})
    guards_mod.clear_seen()
    assert events[-1]["type"] == "done"
    assert recorded["provider"] == "" and recorded["elapsed_ms"] >= 0
    assert recorded["sample"]["trace_id"] == events[-1]["trace_id"]
    assert recorded["sample"]["question"] == "中标通知书效力？"


def test_feedback_down_records_via_wrapper(settings_factory, monkeypatch) -> None:
    settings = settings_factory(ONLINE_EVAL_ENABLED="true")
    client = TestClient(create_app(settings))
    recorded: dict = {}

    class FakeOnline:
        def record_feedback_down(self, **kw):
            recorded.update(kw)

    monkeypatch.setattr(app_online_mod, "get_online", lambda: FakeOnline())

    class FakeFb:
        def insert_feedback(self, *args, **kwargs):
            return "fb-1"

    import bidqa.api.conversations as conv_mod
    monkeypatch.setattr(conv_mod, "build_feedback_store", lambda: FakeFb())
    resp = client.post("/api/feedback", json={"question": "q", "rating": "down",
                                              "answer": "a", "trace_id": "t1"})
    assert resp.status_code == 200
    assert recorded["trace_id"] == "t1" and recorded["question"] == "q"
    assert recorded["answer"] == "a"


# ── B4 限流 / 4091 幂等 ──
def test_rate_limit_429_with_retry_after(client, monkeypatch) -> None:
    patch_service(monkeypatch, chat=FakeChat(tools_script=[text_msg("ok")]))
    limiter = SlidingWindowLimiter(max_requests=1)
    # 端点经 rate_identity 选桶（用户体系后按身份分桶）；此处钉死单次配额桶
    monkeypatch.setattr(stream_mod, "rate_identity",
                        lambda request, settings: (limiter, "test"))
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
    from bidqa.api.chat.schema import ChatStreamRequest
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
    """落库记录器：捕获 upsert 的 user_id 归属与 messages 全文。"""

    def __init__(self):
        self.upserts: list[dict] = []

    def get_conversation(self, conv_id, viewer_user_id=None, machine_domain=False):
        # 可见域参数随 P1-1 端点归属校验加入（恒 None=新会话，放行创建）
        return None

    def upsert_conversation(self, conv_id, title, messages, provider="", user_id=None):
        self.upserts.append({"conv_id": conv_id, "user_id": user_id,
                             "messages": messages})


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
    history_mod.guest_history.clear()
    try:
        events = stream_events(client2, {"message": "问1", "conversation_id": conv},
                               headers={"Authorization": "Bearer tg"})
        assert events[-1]["type"] == "done"
        assert pg.upserts == []  # 游客不落库
        assert history_mod.guest_history.load(conv) == [
            {"role": "user", "content": "问1"}, {"role": "assistant", "content": "答1"}]
        assert events[-1]["conversation_id"] == conv
    finally:
        history_mod.guest_history.clear()


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


# ── 会话落库补全收尾结构（schema_v 2：assistant item 附 done+ts，刷新后回放来源/工具/图表）──
def test_persist_attaches_done_for_restore(settings_factory, monkeypatch):
    """v2 落库：assistant item 附 done（与 SSE done 同一 build_done 产物）+ts；user item 保持二元。"""
    from bidqa.auth.identity import Identity

    ident = Identity("user", "22222222-2222-2222-2222-222222222223", "bob", "t-done")
    client2 = _armed_stream_client(settings_factory, monkeypatch, ident)
    pg = RecordingPg()
    chat = FakeChat(tools_script=[
        tool_msg("search_knowledge", {"query": "中标通知书"}),
        text_msg("依据材料[1]作答。"),
    ])
    patch_service(monkeypatch, chat=chat, pg=pg)
    conv = "22222222-2222-2222-2222-222222222223"
    events = stream_events(client2, {"message": "中标通知书效力？", "conversation_id": conv},
                           headers={"Authorization": "Bearer t-done"})
    done = events[-1]
    assert done["type"] == "done" and done["tool_name"] == "search_knowledge"
    upsert = pg.upserts[-1]
    items = upsert["messages"]["items"]
    assert upsert["messages"]["schema_v"] == 2
    assert [m["role"] for m in items] == ["user", "assistant"]
    assert "done" not in items[0] and "ts" not in items[0]  # user item 二元不变
    stored = items[-1]
    assert stored["content"] == "依据材料[1]作答。" and stored["ts"] > 0
    assert stored["done"]["trace_id"] == done["trace_id"]  # 与 SSE done 同构（wiring 亦覆盖）
    assert stored["done"]["tool_name"] == "search_knowledge"
    assert stored["done"]["rounds"] == done["rounds"]
    assert isinstance(stored["done"]["elapsed_ms"], int)  # 真实测量值（假模型可亚毫秒=0，不做 >0 脆断言）
    assert stored["done"]["sources"] == done["sources"]  # 逐条来源口径一致（含聚合口径条目）
    # WP5b：轨迹字段随 done 落库，刷新后回放执行计划/已补查
    assert stored["done"]["schema_v"] == 2
    assert stored["done"]["plan_steps"] == [] and stored["done"]["reflected"] is False


def test_load_history_projects_binary_only():
    """投影卫兵：v2 item 携带的 done/ts 只服务回放，不进模型上下文；v1 旧行照常加载。"""

    class RowPg:
        def __init__(self, messages):
            self._messages = messages

        def get_conversation(self, conv_id):
            return {"id": conv_id, "title": "t", "messages": self._messages,
                    "provider": "", "created_at": None, "updated_at": None}

    v2 = {"schema_v": 2, "items": [
        {"role": "user", "content": "问", "ts": 1700000000000},
        {"role": "assistant", "content": "答", "ts": 1700000001000,
         "done": {"sources": [{"id": "s1", "title": "法"}], "charts": [{"kind": "bar"}],
                  "tool_name": "search_knowledge", "rounds": 2, "trace_id": "abc"}}]}
    _, items, _ = history_mod.load_history(RowPg(v2), "c1")
    assert items == [{"role": "user", "content": "问"},
                     {"role": "assistant", "content": "答"}]
    # v1 旧行（role/content 二元，无 done/ts）读取兼容
    _, items1, _ = history_mod.load_history(RowPg(
        {"schema_v": 1, "items": [{"role": "user", "content": "q"}]}), "c1")
    assert items1 == [{"role": "user", "content": "q"}]


def test_degrade_answer_persists_with_done(settings_factory, monkeypatch):
    """降级直答对称落库：异常分支与主路径同构（done+ts 随 assistant item 落库，
    刷新后降级回答不再整条消失）；段位标签保持降级两套口径（retrieve_ms）。"""
    from bidqa.auth.identity import Identity

    class BrokenChat(FakeChat):
        def chat_tools(self, messages, tools, temperature=0.3):
            raise DataSourceError("chat", "上游连接被重置")

    ident = Identity("user", "22222222-2222-2222-2222-222222222224", "carol", "t-dg")
    client2 = _armed_stream_client(settings_factory, monkeypatch, ident)
    pg = RecordingPg()
    patch_service(monkeypatch, chat=BrokenChat(plain_script=["依据材料[1]直答。"]), pg=pg)
    conv = "22222222-2222-2222-2222-222222222224"
    events = stream_events(client2, {"message": "中标通知书效力？", "conversation_id": conv},
                           headers={"Authorization": "Bearer t-dg"})
    done = events[-1]
    assert done["type"] == "done"
    assert "降级直答" in [e.get("stage") for e in events if e["type"] == "status"]
    assert any(d.startswith("agent_fallback") for d in done["degraded"])
    upsert = pg.upserts[-1]
    items = upsert["messages"]["items"]
    assert upsert["messages"]["schema_v"] == 2
    assert [m["role"] for m in items] == ["user", "assistant"]
    assert "done" not in items[0] and "ts" not in items[0]  # user item 二元不变
    stored = items[-1]
    assert stored["content"] == "依据材料[1]直答。" and stored["ts"] > 0
    assert stored["done"]["trace_id"] == done["trace_id"]  # 与 SSE done 同 id（对账）
    assert isinstance(stored["done"]["elapsed_ms"], int)  # 真实测量值（假模型降级亚毫秒=0，不做 >0 脆断言）
    assert stored["done"]["sources"] == done["sources"]
    assert "retrieve_ms" in stored["done"]["phase_times"]  # 降级两套段名不合并


def test_guest_degrade_caches_history(settings_factory, monkeypatch):
    """游客降级直答：仍不落库（诚实），但进进程内历史缓存（与主路径游客语义一致）。"""
    from bidqa.auth.identity import Identity

    class BrokenChat(FakeChat):
        def chat_tools(self, messages, tools, temperature=0.3):
            raise DataSourceError("chat", "上游连接被重置")

    ident = Identity("guest", None, "guest", "t-gdg")
    client2 = _armed_stream_client(settings_factory, monkeypatch, ident)
    pg = RecordingPg()
    patch_service(monkeypatch, chat=BrokenChat(plain_script=["依据材料[1]直答。"]), pg=pg)
    conv = "11111111-1111-1111-1111-111111111112"
    history_mod.guest_history.clear()
    try:
        events = stream_events(client2, {"message": "问1", "conversation_id": conv},
                               headers={"Authorization": "Bearer t-gdg"})
        assert events[-1]["type"] == "done"
        assert pg.upserts == []  # 游客不落库（降级路径同样诚实）
        assert history_mod.guest_history.load(conv) == [
            {"role": "user", "content": "问1"}, {"role": "assistant", "content": "依据材料[1]直答。"}]
    finally:
        history_mod.guest_history.clear()


# ── 审计 P2-31/P2-32：降级路径对称性（摘要保留 + reset 语义） ──
class SummaryPg(RecordingPg):
    """带既有滚动摘要的落库记录器：验证降级 upsert 不抹摘要。"""

    def __init__(self, summary: str):
        super().__init__()
        self._summary = summary

    def get_conversation(self, conv_id, viewer_user_id=None, machine_domain=False):
        return {"id": conv_id, "title": "t",
                "messages": {"schema_v": 2, "items": [], "summary": self._summary},
                "provider": "deep", "created_at": None, "updated_at": None}


def test_degrade_persists_preserves_prior_summary(settings_factory, monkeypatch):
    """P2-31（审计）：降级落库曾把 prior_summary 硬编码 ""——任一异常降级直答后
    该会话 C7 滚动摘要被永久抹掉（docstring 声称「与主路径同构」不成立）。"""
    from bidqa.auth.identity import Identity

    class BrokenChat(FakeChat):
        def chat_tools(self, messages, tools, temperature=0.3):
            raise DataSourceError("chat", "上游连接被重置")

    ident = Identity("user", "22222222-2222-2222-2222-222222222231", "carol", "t-dgs")
    client2 = _armed_stream_client(settings_factory, monkeypatch, ident)
    pg = SummaryPg("旧摘要：用户在安徽关注空调")
    patch_service(monkeypatch, chat=BrokenChat(plain_script=["依据材料[1]直答。"]), pg=pg)
    conv = "22222222-2222-2222-2222-222222222231"
    stream_events(client2, {"message": "中标通知书效力？", "conversation_id": conv},
                  headers={"Authorization": "Bearer t-dgs"})
    assert pg.upserts, "降级回答应落库"
    assert pg.upserts[-1]["messages"]["summary"] == "旧摘要：用户在安徽关注空调"  # 不被 "" 抹掉


def test_degrade_after_half_stream_emits_reset(settings_factory, monkeypatch):
    """P2-32（审计）：主路径中途异常切降级不发 reset——已流半截 token 不清空，
    降级又整段重发 → 客户端「半截+全文」拼接（与 _stream_plain 断流即收口径相反）。
    修复后 reset 先于降级 status。"""
    from bidqa.auth.identity import Identity

    class HalfStreamChat(FakeChat):
        def chat_stream_round(self, messages, tools, temperature=0.3):
            yield ("content", "半截正文：")
            raise RuntimeError("上游断流")

    ident = Identity("user", "22222222-2222-2222-2222-222222222232", "carol", "t-dgr")
    client2 = _armed_stream_client(settings_factory, monkeypatch, ident)
    patch_service(monkeypatch, chat=HalfStreamChat(plain_script=["降级全文。"]), pg=RecordingPg())
    events = stream_events(client2, {"message": "中标通知书效力？"},
                           headers={"Authorization": "Bearer t-dgr"})
    idx_token = next(i for i, e in enumerate(events)
                     if e["type"] == "token" and "半截" in e.get("text", ""))
    idx_reset = next(i for i, e in enumerate(events) if e["type"] == "reset")
    idx_degrade = next(i for i, e in enumerate(events)
                       if e["type"] == "status" and e.get("stage") == "降级直答")
    assert idx_token < idx_reset < idx_degrade  # 半截先清空，降级全文才下发
    full = "".join(e.get("text", "") for e in events if e["type"] == "token")
    assert "降级全文。" in full

def test_guest_provider_ignored_server_side(settings_factory, monkeypatch):
    """P1-27（审计）：游客 provider 曾被透传——localStorage 偏好绕过 UI 锁直传
    命名档（docs/02 A14 游客锁默认档）。服务端是门禁真源：游客一律主槽位。"""
    from bidqa.auth.identity import Identity

    seen: list[str | None] = []
    chat = FakeChat(tools_script=[text_msg("答")])

    def capture(provider):
        seen.append(provider)
        return chat

    ident = Identity("guest", None, "guest", "tg-p27")
    client2 = _armed_stream_client(settings_factory, monkeypatch, ident)
    patch_service(monkeypatch, chat=chat, pg=RecordingPg())
    monkeypatch.setattr(worker_mod, "build_chat_for", capture)  # 覆盖以捕获 provider
    monkeypatch.setattr(availability_mod, "build_chat_for", capture)
    history_mod.guest_history.clear()
    try:
        events = stream_events(client2, {"message": "问", "provider": "ghost-profile"},
                               headers={"Authorization": "Bearer tg-p27"})
        assert events[-1]["type"] == "done"
        assert seen and all(p is None for p in seen),             f"游客 provider 应被服务端忽略，实际 {seen}（原透传=红）"
    finally:
        history_mod.guest_history.clear()


def test_metrics_note_request_real_status(client, settings_factory, monkeypatch):
    """P2-5（审计）：note_request 曾在限流/幂等/5031 判定**之前**写死 200——
    5031（降级）等被计成成功，requests[].errors 恒 0。收口后按真实状态码计数。"""
    from bidqa.obs import logs as obs_mod

    obs_mod.request_stats.pop("/api/chat/stream", None)
    chat = FakeChat(tools_script=[text_msg("答")])
    patch_service(monkeypatch, chat=chat, pg=RecordingPg())
    events = stream_events(client, {"message": "问", "client_msg_id": "st-1"})
    assert events[-1]["type"] == "done"
    stats = obs_mod.request_stats["/api/chat/stream"]
    assert stats["count"] == 1 and stats["errors"] == 0
    # 5031：chat 不可用 → 真实 503 → errors 计数（原写死 200 → 恒 0，红）
    monkeypatch.setattr(availability_mod, "build_chat_for", lambda provider: None)
    guards_mod.clear_seen()
    r503 = client.post("/api/chat/stream", json={"message": "问", "client_msg_id": "st-2"})
    assert r503.status_code == 503
    stats = obs_mod.request_stats["/api/chat/stream"]
    assert stats["count"] == 2 and stats["errors"] == 1
