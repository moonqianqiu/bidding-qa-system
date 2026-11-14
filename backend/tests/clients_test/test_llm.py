"""M1-7：通用 OpenAI-compatible LLM 适配器（respx 模拟四方法）。"""

import json as _json

import httpx
import pytest
import respx

from bidqa.clients.llm import ChatClient
from bidqa.clients.siliconflow import DataSourceError

BASE = "https://chat.example/v1"
MESSAGES = [{"role": "user", "content": "你好"}]


def sse_lines(*chunks: dict) -> httpx.Response:
    body = "".join(f"data: {_json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


@respx.mock
def test_chat_non_stream():
    respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content": "答案"}}]}))
    client = ChatClient(BASE, "sk", "test-model")
    assert client.chat(MESSAGES) == "答案"
    body = respx.calls.last.request.read().decode()
    assert "test-model" in body and "你好" in body


@respx.mock
def test_chat_stream_parses_sse():
    respx.post(f"{BASE}/chat/completions").mock(return_value=sse_lines(
        {"choices": [{"delta": {"content": "你"}}]},
        {"choices": [{"delta": {"content": "好"}}]},
    ))
    client = ChatClient(BASE, "sk", "m")
    assert list(client.chat_stream(MESSAGES)) == ["你", "好"]


@respx.mock
def test_chat_stream_thinking_splits_streams():
    respx.post(f"{BASE}/chat/completions").mock(return_value=sse_lines(
        {"choices": [{"delta": {"reasoning_content": "思考"}}]},
        {"choices": [{"delta": {"content": "答案"}}]},
    ))
    client = ChatClient(BASE, "sk", "thinker")
    out = list(client.chat_stream_thinking(MESSAGES, thinking_model="thinker"))
    assert out == [("thinking", "思考"), ("content", "答案")]
    body = respx.calls.last.request.read().decode()
    assert "thinker" in body  # 深度思考=切换模型


@respx.mock
def test_chat_raw_passthrough():
    respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(200, json={
        "id": "x", "choices": []}))
    client = ChatClient(BASE, "sk", "m")
    payload = {"model": "m", "messages": MESSAGES, "temperature": 0}
    assert client.chat_raw(payload)["id"] == "x"


@respx.mock
def test_500_raises_source_error():
    respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(500, json={}))
    client = ChatClient(BASE, "sk", "m")
    with pytest.raises(DataSourceError):
        client.chat(MESSAGES)


# ── 上游 429：仅 sensenova 中转端点重试，其他端点立即报错（用户口径）──
SENSENOVA = "https://token.sensenova.cn/v1"


@respx.mock
def test_sensenova_429_stream_retries_before_first_byte(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("bidqa.clients.llm.time.sleep", lambda s: sleeps.append(s))
    route = respx.post(f"{SENSENOVA}/chat/completions").mock(side_effect=[
        httpx.Response(429, json={"error": {"message": "tpm limit"}},
                       headers={"Retry-After": "1"}),
        sse_lines({"choices": [{"delta": {"content": "你"}}]},
                  {"choices": [{"delta": {"content": "好"}}]}),
    ])
    client = ChatClient(SENSENOVA, "sk", "m")
    assert list(client.chat_stream(MESSAGES)) == ["你", "好"]
    assert route.call_count == 2
    assert sleeps == [1.0]


@respx.mock
def test_sensenova_429_tool_round_retries(monkeypatch):
    monkeypatch.setattr("bidqa.clients.llm.time.sleep", lambda s: None)
    route = respx.post(f"{SENSENOVA}/chat/completions").mock(side_effect=[
        httpx.Response(429, json={}),
        sse_lines({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "search_database",
                                                  "arguments": "{}"}}]}}]}),
    ])
    client = ChatClient(SENSENOVA, "sk", "m")
    events = list(client.chat_stream_round(MESSAGES, tools=[]))
    assert route.call_count == 2
    assert events[0][0] == "tool" and events[0][1][2] == "search_database"


@respx.mock
def test_sensenova_429_exhausts_after_max_attempts(monkeypatch):
    monkeypatch.setattr("bidqa.clients.llm.time.sleep", lambda s: None)
    route = respx.post(f"{SENSENOVA}/chat/completions").mock(
        return_value=httpx.Response(429, json={"error": {"message": "tpm limit"}}))
    client = ChatClient(SENSENOVA, "sk", "m")
    with pytest.raises(DataSourceError, match="429"):
        list(client.chat_stream(MESSAGES))
    assert route.call_count == 4  # 首发 + 3 次重试


@respx.mock
def test_other_host_429_stream_raises_without_retry():
    route = respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(429, json={}))
    client = ChatClient(BASE, "sk", "m")
    with pytest.raises(DataSourceError, match="429"):
        list(client.chat_stream(MESSAGES))
    assert route.call_count == 1


@respx.mock
def test_other_host_429_non_stream_raises_without_retry():
    route = respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(429, json={}))
    client = ChatClient(BASE, "sk", "m")
    with pytest.raises(DataSourceError, match="429"):
        client.chat(MESSAGES)
    assert route.call_count == 1


@respx.mock
def test_sensenova_429_non_stream_retries(monkeypatch):
    monkeypatch.setattr("bidqa.clients.llm.time.sleep", lambda s: None)
    route = respx.post(f"{SENSENOVA}/chat/completions").mock(side_effect=[
        httpx.Response(429, json={}),
        httpx.Response(200, json={"choices": [{"message": {"content": "答案"}}]}),
    ])
    client = ChatClient(SENSENOVA, "sk", "m")
    assert client.chat(MESSAGES) == "答案"
    assert route.call_count == 2


# ── 上游空流（200 但 0 个 data 块，2026-11-10 现场 trace 1023ef75bd21）：──
# 仅自建中转端点补发一次；其他端点快速失败（与 429 同一端点白名单策略）。


@respx.mock
def test_sensenova_empty_stream_retries_once(monkeypatch):
    monkeypatch.setattr("bidqa.clients.llm.time.sleep", lambda s: sleeps.append(s))
    sleeps: list[float] = []
    route = respx.post(f"{SENSENOVA}/chat/completions").mock(side_effect=[
        sse_lines(),  # 200 空流：只有 [DONE]，无任何 data 块
        sse_lines({"choices": [{"delta": {"content": "你"}}]}),
    ])
    client = ChatClient(SENSENOVA, "sk", "m")
    assert list(client.chat_stream(MESSAGES)) == ["你"]
    assert route.call_count == 2 and sleeps == [1.0]


@respx.mock
def test_sensenova_empty_stream_twice_gives_up(monkeypatch):
    monkeypatch.setattr("bidqa.clients.llm.time.sleep", lambda s: None)
    route = respx.post(f"{SENSENOVA}/chat/completions").mock(
        return_value=sse_lines())  # 持续空流
    client = ChatClient(SENSENOVA, "sk", "m")
    assert list(client.chat_stream(MESSAGES)) == []  # 不抛错，交上层兜底
    assert route.call_count == 2  # 首发 + 1 次补发，有界


@respx.mock
def test_other_host_empty_stream_fails_fast():
    route = respx.post(f"{BASE}/chat/completions").mock(return_value=sse_lines())
    client = ChatClient(BASE, "sk", "m")
    assert list(client.chat_stream_round(MESSAGES, tools=[])) == []
    assert route.call_count == 1  # 非中转端点不重试（禁止全局重试藏故障）


# ── 主槽位输出预算（CHAT_MAX_TOKENS）：思考型上游先烧 reasoning 再出正文，──
# 预算不足时正文为空（2026-11-11 trace 9a86dbb765a9 现场复现）。

@respx.mock
def test_max_tokens_injected_all_paths():
    route = respx.post(f"{BASE}/chat/completions").mock(side_effect=[
        httpx.Response(200, json={"choices": [{"message": {"content": "答案"}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": "答案"}}]}),
        sse_lines({"choices": [{"delta": {"content": "好"}}]}),
    ])
    client = ChatClient(BASE, "sk", "m", max_tokens=8192)
    assert client.chat(MESSAGES) == "答案"
    assert _json.loads(route.calls[0].request.read().decode())["max_tokens"] == 8192
    tools_payload = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
    client.chat_tools(MESSAGES, tools_payload)
    assert _json.loads(route.calls[1].request.read().decode())["max_tokens"] == 8192
    assert list(client.chat_stream(MESSAGES)) == ["好"]
    assert _json.loads(route.calls[2].request.read().decode())["max_tokens"] == 8192


@respx.mock
def test_max_tokens_none_omitted():
    respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content": "答案"}}]}))
    client = ChatClient(BASE, "sk", "m")  # 缺省：不传 max_tokens（上游默认）
    client.chat(MESSAGES)
    assert "max_tokens" not in _json.loads(respx.calls.last.request.read().decode())


@respx.mock
def test_max_tokens_not_injected_into_chat_raw():
    respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(200, json={
        "id": "x", "choices": []}))
    client = ChatClient(BASE, "sk", "m", max_tokens=8192)
    payload = {"model": "m", "messages": MESSAGES, "temperature": 0}
    client.chat_raw(payload)  # 评测/调试原样透传，预算不注入
    body = _json.loads(respx.calls.last.request.read().decode())
    assert "max_tokens" not in body


@respx.mock
def test_extra_body_merged_into_budgeted_but_not_raw():
    respx.post(f"{BASE}/chat/completions").mock(side_effect=[
        httpx.Response(200, json={"choices": [{"message": {"content": "答案"}}]}),
        httpx.Response(200, json={"id": "x", "choices": []}),
    ])
    client = ChatClient(BASE, "sk", "m",
                        extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    client.chat(MESSAGES)
    body = _json.loads(respx.calls[0].request.read().decode())
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    client.chat_raw({"model": "m", "messages": MESSAGES})
    raw_body = _json.loads(respx.calls[1].request.read().decode())
    assert "chat_template_kwargs" not in raw_body  # 原样透传不被附加字段污染
