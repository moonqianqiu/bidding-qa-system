"""三槽位通用 OpenAI-compatible LLM 适配器（docs/01 clients 层；C8）。

无预置厂商枚举：{base_url, api_key, model} 即插即用。四方法接口：
chat / chat_stream / chat_stream_thinking / chat_raw（评测用原样透传）。
流式解析 SSE data: 行；reasoning_content 与正文分流（深度思考）。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from typing import Any

import httpx

from bidqa.clients.siliconflow import DataSourceError, _post_with_retry

# 上游 429 退避：只对已知会频繁限流的自有中转端点重试，其他端点 429 立即报错（不盲目重试）
RETRY_429_HOST = "token.sensenova.cn"
_RETRY_429_MAX_ATTEMPTS = 4
_RETRY_429_TOTAL_BUDGET = 45.0   # 重试等待总预算（秒），避免限流时长时间挂住流
_RETRY_429_MAX_DELAY = 30.0
# 上游空流（200 但 0 个 data 块，2026-11-10 trace 1023ef75bd21 现场复现）：
# 与 429 同源的自建中转偶发故障。补发有界且仅在首字节未吐出时（零 chunk ⇒ 零 yield，
# 重发绝不重复文本）；其他端点快速失败——与 429 同一端点白名单，不加全局重试藏故障。
_EMPTY_STREAM_MAX_RETRIES = 1
_EMPTY_STREAM_RETRY_DELAY = 1.0

logger = logging.getLogger("bidqa.clients.llm")


def retry_429_eligible(base_url: str) -> bool:
    """该端点的 429 是否值得重试（仅 token.sensenova.cn 及其子域）。"""
    host = (httpx.URL(base_url).host or "").lower()
    return host == RETRY_429_HOST or host.endswith("." + RETRY_429_HOST)


def _retry_429_delay(headers: Any, attempt: int) -> float:
    """优先尊重 Retry-After，缺失则指数退避（2/4/8s）；单次封顶 30s。"""
    try:
        delay = float(headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        delay = 2.0 * (2 ** attempt)
    return max(0.0, min(delay, _RETRY_429_MAX_DELAY))


class ChatClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120.0,
                 max_tokens: int | None = None,
                 extra_body: dict[str, Any] | None = None):
        self.model = model
        self._max_tokens = max_tokens
        # 通用 OpenAI-compatible 附加字段（模型族开关，如关闭思考）；chat_raw 原样透传不受影响
        self.extra_body = dict(extra_body or {})
        self._retry_429 = retry_429_eligible(base_url)
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            trust_env=False,
        )

    def _budgeted(self, payload: dict) -> dict:
        """注入主槽位输出预算（CHAT_MAX_TOKENS）：思考型上游先烧 reasoning 再出正文，
        预算不足时正文为空（2026-11-11 trace 9a86dbb765a9：图表正常、正文空）；
        None = 不传，维持上游默认。chat_raw 评测透传不受影响。"""
        merged = {**self.extra_body, **payload}
        if self._max_tokens:
            merged["max_tokens"] = self._max_tokens
        return merged

    # ── 非流式 ──
    def chat(self, messages: list[dict], temperature: float = 0.3) -> str:
        data = _post_with_retry(
            self._client, "/chat/completions",
            self._budgeted({"model": self.model, "messages": messages,
                            "temperature": temperature}),
            source="chat", retry_429=self._retry_429,
        )
        return data["choices"][0]["message"]["content"] or ""

    def chat_raw(self, payload: dict) -> dict:
        """评测/调试用：原样透传 payload，返回完整响应 JSON。"""
        return _post_with_retry(self._client, "/chat/completions", payload, source="chat",
                                retry_429=self._retry_429)

    def chat_tools(self, messages: list[dict], tools: list[dict],
                   temperature: float = 0.3) -> dict:
        """function-calling 轮次调用（C3 工具循环专用）：返回 assistant message dict
        （{role, content, tool_calls?}，tool_calls[].function.arguments 为 JSON 字符串）。"""
        data = _post_with_retry(
            self._client, "/chat/completions",
            self._budgeted({"model": self.model, "messages": messages, "tools": tools,
                            "temperature": temperature}),
            source="chat", retry_429=self._retry_429,
        )
        return data["choices"][0]["message"]

    def chat_stream_round(self, messages: list[dict], tools: list[dict],
                          temperature: float = 0.3) -> Iterator[tuple[str, Any]]:
        """流式 function-calling 轮次（C6 真流式核心）：
        yield ("content", 文本增量) —— 最终回答的打字机来源；
        yield ("tool", (index, id, name, arguments片段)) —— 由调用方拼装 tool_calls。
        流结束后由调用方判断走工具轮还是收口。"""
        payload = {"model": self.model, "messages": messages, "tools": tools,
                   "temperature": temperature}
        for data in self._stream_chunks(payload):
            delta = (data.get("choices") or [{}])[0].get("delta", {})
            if delta.get("content"):
                yield ("content", delta["content"])
            for tc in delta.get("tool_calls") or []:
                fn = tc.get("function", {})
                yield ("tool", (tc.get("index", 0), tc.get("id"),
                                fn.get("name"), fn.get("arguments")))

    # ── 流式 ──
    def _stream_chunks(self, payload: dict) -> Iterator[dict]:
        """流式请求单一入口：429 仅在可重试端点、且**首字节尚未吐出**时退避重试，
        因此不会重复输出已流出的文本；其他状态码立即抛 DataSourceError（5031）。"""
        payload = self._budgeted({**payload, "stream": True})
        attempt = 0
        spent = 0.0
        while True:
            with self._client.stream("POST", "/chat/completions", json=payload) as resp:
                if resp.status_code == 200:
                    chunks = 0
                    for line in resp.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break
                        chunks += 1
                        yield json.loads(chunk)
                    if chunks == 0:
                        logger.warning("上游流式响应为空 model=%s attempt=%d", self.model, attempt + 1)
                        # 空流补发：仅自建中转端点，且零 yield 时才安全（无已吐文本可重复）
                        if self._retry_429 and attempt < _EMPTY_STREAM_MAX_RETRIES:
                            attempt += 1
                            time.sleep(_EMPTY_STREAM_RETRY_DELAY)
                            continue
                    return
                body = resp.read().decode("utf-8", "replace")
                status, headers = resp.status_code, resp.headers
            if status == 429 and self._retry_429 and attempt < _RETRY_429_MAX_ATTEMPTS - 1:
                delay = _retry_429_delay(headers, attempt)
                if spent + delay <= _RETRY_429_TOTAL_BUDGET:
                    logger.warning("chat 429 退避重试 model=%s attempt=%d delay=%.1fs",
                                   self.model, attempt + 1, delay)
                    time.sleep(delay)
                    spent += delay
                    attempt += 1
                    continue
            raise DataSourceError("chat", f"HTTP {status}: {body[:200]}")

    def chat_stream(self, messages: list[dict], temperature: float = 0.3) -> Iterator[str]:
        """正文增量（跳过 reasoning_content）。"""
        for chunk in self._stream_chunks({"model": self.model, "messages": messages,
                                         "temperature": temperature}):
            delta = chunk["choices"][0].get("delta", {})
            content = delta.get("content")
            if content:
                yield content

    def chat_stream_thinking(self, messages: list[dict], thinking_model: str,
                             temperature: float = 0.3) -> Iterator[tuple[str, str]]:
        """深度思考：切换思考模型，yield ("thinking"|"content", 文本)。"""
        payload = {"model": thinking_model, "messages": messages, "temperature": temperature}
        for chunk in self._stream_chunks(payload):
            delta = chunk["choices"][0].get("delta", {})
            if delta.get("reasoning_content"):
                yield "thinking", delta["reasoning_content"]
            if delta.get("content"):
                yield "content", delta["content"]

    def ping(self) -> bool:
        try:
            resp = self._client.get("/models")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def close(self) -> None:
        self._client.close()
