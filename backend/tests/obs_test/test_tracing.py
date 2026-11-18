"""OTel span 埋点契约（WP3；docs/01 §6 可观测性段）。

四条契约：
1. 未配置 OTLP 端点：setup_tracing 返回 None，全局 tracer provider 原样（no-op 语义）；
2. InMemory 导出：span 树父子关系/属性正确；敏感内容不入属性（白名单 + 截断纵深）；
3. 异常路径：span 记录异常事件 + 状态 ERROR，异常原样上抛；
4. 最小 run 集成：run_chat_job 一次问答产出 chat.turn 根 span 与 persist 子 span
   （中间层 agent.round / llm.chat_stream / tool.* 同树可见）。

全局 provider 单次生效限制：opentelemetry 用 Once 单次闸钉死「provider 只能设一次」
（公开 API 二次 set 仅告警；私有 _set_tracer_provider 二次调用同样 no-op）——进程内
没有任何公开路径可还原全局态。测试要的是「每用例从未配置态出发」，fixture 直接换新
Once 并清 _TRACER_PROVIDER（等价重开进程的全局态；与 conftest 清 lru_cache 同一
隔离精神）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from opentelemetry.util import _once
from pydantic import BaseModel, Field

from bidqa.obs import tracing
from bidqa.obs.tracing import current_trace_id, setup_tracing, shutdown_tracing, span


@pytest.fixture(autouse=True)
def _reset_global_provider():
    """每用例前后把全局 provider 复位到未配置态（见模块 docstring 的私有 API 说明）。"""

    def _reset() -> None:
        shutdown_tracing()
        otel_trace._TRACER_PROVIDER_SET_ONCE = _once.Once()
        otel_trace._TRACER_PROVIDER = None

    _reset()
    yield
    _reset()


# ── 契约 1：未配置端点 = no-op ──
def test_no_endpoint_returns_none_and_provider_untouched(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    before = otel_trace.get_tracer_provider()
    assert setup_tracing() is None
    assert setup_tracing(None) is None
    assert otel_trace.get_tracer_provider() is before  # 全局 provider 未被改动
    assert current_trace_id() == ""  # 未启用：无 trace id 可关联


def test_settings_without_endpoint_stays_noop(settings_factory) -> None:
    before = otel_trace.get_tracer_provider()
    assert setup_tracing(settings_factory()) is None
    assert otel_trace.get_tracer_provider() is before


def test_env_var_endpoint_initializes_without_settings(monkeypatch) -> None:
    """无 Settings 时只看进程环境变量（与公开 API 默认行为一致的 OTLP 配置路径）。"""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel.test:4318")
    provider = setup_tracing()
    assert provider is not None
    assert (provider.resource.attributes["service.name"] == "bidqa")  # 默认服务名
    shutdown_tracing()
    assert tracing._provider is None  # 幂等复位


def test_settings_endpoint_initializes(settings_factory) -> None:
    provider = setup_tracing(settings_factory(
        OTEL_EXPORTER_OTLP_ENDPOINT="http://otel.test:4318"))
    assert provider is not None
    resource = provider.resource.attributes
    assert resource["service.name"] == "bidqa"
    assert resource["service.version"]  # 项目版本（importlib.metadata）


# ── 契约 2：span 树 + 属性 + 隐私 ──
def test_span_tree_parent_child_and_attrs(settings_factory) -> None:
    exporter = InMemorySpanExporter()
    provider = setup_tracing(settings_factory(OTEL_EXPORTER_OTLP_ENDPOINT="http://otel.test:4318"), exporter=exporter)
    assert provider is not None
    with span("chat.turn", question_len=10, provider="deepseek", is_guest=True) as root:
        with span("persist", items=2, schema_v=2):
            assert current_trace_id() == f"{root.get_span_context().trace_id:032x}"
    spans = exporter.get_finished_spans()
    root_rec = next(s for s in spans if s.name == "chat.turn")
    child = next(s for s in spans if s.name == "persist")
    assert len(spans) == 2
    assert child.parent is not None
    assert child.parent.span_id == root_rec.get_span_context().span_id  # 父子关系
    assert (root_rec.get_span_context().trace_id
            == child.get_span_context().trace_id)  # 同一 trace
    assert root_rec.attributes["question_len"] == 10
    assert root_rec.attributes["provider"] == "deepseek"
    assert root_rec.attributes["is_guest"] is True
    assert child.attributes["items"] == 2
    assert child.attributes["schema_v"] == 2


def test_privacy_whitelist_and_attr_truncation(settings_factory) -> None:
    """白名单纪律（调用点只放元数据）+ 截断纵深（超长字符串属性被 span() 截断）。"""
    exporter = InMemorySpanExporter()
    setup_tracing(settings_factory(OTEL_EXPORTER_OTLP_ENDPOINT="http://otel.test:4318"), exporter=exporter)
    secret = "机密问题原文：某市交警支队采购项目预算多少？sk-live-abcdef123456"
    with span("chat.turn", question_len=len(secret)):  # 只放长度，绝不放原文
        with span("llm.chat_stream", model="test-model", round=1, outcome="text"):
            pass
    with span("rag.retrieve", top_k=5, note="x" * 500):
        pass
    payload = json.dumps([(s.name, dict(s.attributes or {}))
                          for s in exporter.get_finished_spans()], ensure_ascii=False)
    assert secret not in payload
    assert "sk-live" not in payload
    rag = next(s for s in exporter.get_finished_spans() if s.name == "rag.retrieve")
    assert len(rag.attributes["note"]) == tracing.MAX_ATTR_LEN  # 截断上限


# ── 契约 3：异常路径 ──
def test_span_error_status_and_reraise(settings_factory) -> None:
    exporter = InMemorySpanExporter()
    setup_tracing(settings_factory(OTEL_EXPORTER_OTLP_ENDPOINT="http://otel.test:4318"), exporter=exporter)
    with pytest.raises(RuntimeError, match="数据源故障"):
        with span("chat.turn", question_len=3):
            raise RuntimeError("数据源故障")
    (record,) = exporter.get_finished_spans()
    assert record.status.status_code == StatusCode.ERROR
    assert record.events and record.events[0].name == "exception"


# ── 契约 3 补充：异常内容不入 span（隐私白名单纵深）──
def test_span_error_does_not_leak_exception_message(settings_factory) -> None:
    """异常 message 可能夹带问题原文/密钥（上游报错回显等），span 只记类型名与
    栈帧，绝不记录异常文本（record_exception 会把 str(exc) 写进事件 → 不用它）。"""
    exporter = InMemorySpanExporter()
    setup_tracing(settings_factory(OTEL_EXPORTER_OTLP_ENDPOINT="http://otel.test:4318"), exporter=exporter)
    secret = "用户问题原文 sk-live-abcdef"
    with pytest.raises(RuntimeError):
        with span("chat.turn", question_len=3):
            raise RuntimeError(f"上游失败：{secret}")
    (record,) = exporter.get_finished_spans()
    blob = json.dumps([(e.name, dict(e.attributes or {})) for e in record.events],
                      ensure_ascii=False, default=str)
    blob += str(record.status.description or "")
    assert secret not in blob
    assert "sk-live" not in blob
    assert record.events and record.events[0].attributes.get("exception.type") == "RuntimeError"


# ── 契约 1 补充：未配置时 span() 本身可用且零副作用 ──
def test_span_without_provider_is_noop(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    before = otel_trace.get_tracer_provider()
    with span("chat.turn", question_len=1) as s:
        s.set_attribute("x", 1)
        assert current_trace_id() == ""
    assert otel_trace.get_tracer_provider() is before


# ── 契约 4：最小 run 集成（fake 桩打 worker 缝，不打真实 API）──
def test_minimal_run_emits_chat_turn_tree(settings_factory, monkeypatch) -> None:
    from bidqa.application.chat import worker as worker_mod
    from bidqa.application.chat.worker import ChatJob, run_chat_job
    from bidqa.tools.base import BaseTool, ToolRegistry, tool_result

    class Args(BaseModel):
        query: str = Field(min_length=1)

    class FakeSearchTool(BaseTool[Args]):
        name = "search_knowledge"
        description = "知识库检索（fake）"
        args_model = Args

        def run(self, args: Args) -> dict:
            return tool_result(text=f"[1] {args.query} 相关材料",
                               sources=[{"id": "c1", "title": "t", "score": 0.9}])

    class FakeChat:
        model = "test-model"

        def __init__(self) -> None:
            # 轮 1：点名工具；轮 2：文本收口
            self.script: list[list[Any]] = [
                [("tool", (0, "c1", "search_knowledge", '{"query": "预算"}'))],
                [("content", "依据材料[1]，中标金额为 100 万元。")],
            ]

        def chat_stream_round(self, messages, tools, temperature=0.3):
            for item in self.script.pop(0):
                yield item

    class FakeStore:
        def __init__(self) -> None:
            self.rows: dict[str, dict] = {}

        def get_conversation(self, conv_id: str, **_kw: Any):
            return self.rows.get(conv_id)

        def upsert_conversation(self, conv_id, title, messages, provider,
                                user_id=None) -> None:
            self.rows[conv_id] = {"conversation_id": conv_id, "title": title,
                                  "messages": messages, "provider": provider}

    registry = ToolRegistry()
    registry.register(FakeSearchTool())
    store = FakeStore()
    monkeypatch.setattr(worker_mod, "build_chat_for", lambda provider: FakeChat())
    monkeypatch.setattr(worker_mod, "build_registry", lambda *a, **k: registry)
    monkeypatch.setattr(worker_mod, "build_retriever", lambda: None)
    monkeypatch.setattr(worker_mod, "build_conversation_store", lambda: store)
    monkeypatch.setattr(worker_mod, "build_web_clients", lambda: {})
    monkeypatch.setattr(worker_mod, "build_neo", lambda: None)
    monkeypatch.setattr(worker_mod, "build_bp_repo", lambda: None)
    monkeypatch.setattr(worker_mod, "build_reranker", lambda: None)

    exporter = InMemorySpanExporter()
    setup_tracing(settings_factory(OTEL_EXPORTER_OTLP_ENDPOINT="http://otel.test:4318"), exporter=exporter)
    question = "北京市2024年电梯采购项目中标金额是多少"  # 地区/时间/标的物齐备，避开澄清闸门
    job = ChatJob(message=question, provider="test", is_guest=False)
    run_chat_job(job, lambda _t, _p: None)

    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert "chat.turn" in names and "persist" in names
    assert "agent.round" in names and "llm.chat_stream" in names
    assert "tool.search_knowledge" in names
    root = next(s for s in spans if s.name == "chat.turn")
    assert root.parent is None  # 根 span
    assert root.attributes["question_len"] == len(question)
    assert root.attributes["provider"] == "test"
    assert root.attributes["lang"] == "zh"
    assert root.attributes["has_conversation"] is False
    persist = next(s for s in spans if s.name == "persist")
    assert persist.attributes["schema_v"] == 2
    assert persist.attributes["items"] == 2  # 问答对
    tool_span = next(s for s in spans if s.name == "tool.search_knowledge")
    assert tool_span.attributes["outcome"] == "ok"
    assert tool_span.attributes["elapsed_ms"] >= 0
    assert tool_span.attributes["arguments_len"] > 0
    # 每轮 LLM 调用一个 llm.chat_stream；轮 outcome 如实
    llm_spans = [s for s in spans if s.name == "llm.chat_stream"]
    assert len(llm_spans) == 2
    assert [s.attributes["outcome"] for s in llm_spans] == ["tool_calls", "text"]
