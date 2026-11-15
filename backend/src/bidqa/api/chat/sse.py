"""SSE 传输机制（docs/01 §3.4）：事件队列 + ping 心跳 + seq 单调 + data: JSON 帧。

从 chat_stream 归位（重构 C3）：纯机制、零业务——工作线程经 emit 投递事件，
generate 迭代器按「事件即下发、空闲 15s 发 ping」泵出。契约序
（status*→thinking*→token*→done|error）由 AgentLoop/worker 保证，本层不重排。
错误体（_error）与收尾（_result）是 worker→传输的内部约定信号。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from queue import Empty, Queue
from typing import Any, Callable, AsyncIterator

PING_INTERVAL = 15.0  # 心跳间隔（防反向代理空闲断连，Nginx 默认 60s）

_channels: set["SseChannel"] = set()  # 活动通道注册表（停机广播用，审计 P1-8）
_channels_lock = threading.Lock()


def sse_line(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


def active_channel_count() -> int:
    with _channels_lock:
        return len(_channels)


def broadcast_shutdown() -> int:
    """停机②（docs/01 §6 四步；审计 P1-8）：向所有活动 SSE 通道广播 error 5031——
    客户端收到的是结构化「服务端维护」而非裸断连。"""
    from bidqa.errors import EC_SOURCE_DEGRADED

    with _channels_lock:
        targets = list(_channels)
    for ch in targets:
        ch.emit("_error", {"error_code": EC_SOURCE_DEGRADED,
                           "message": "服务停机维护中，请稍后重试"})
    return len(targets)


class SseChannel:
    """工作线程 → StreamingResponse 的事件通道（单连接生命周期）。

    **异步消费**（审计 P1-21）：lines() 是 async 生成器——同步迭代器会让
    Starlette iterate_in_threadpool 给每条 SSE 连接占 1 个 AnyIO 线程
    （默认 40，空闲 get 也占着），40+ 并发流会饿死所有同步 def 端点。
    工作线程 emit 经 call_soon_threadsafe 投递 asyncio 队列；绑定前（响应
    尚未开始迭代）的投递落同步缓冲，首次迭代时在锁内原子搬运（不重不漏）。
    """

    def __init__(self, ping_interval: float = PING_INTERVAL):
        self.queue: Queue = Queue()  # 绑定前缓冲 + 未绑定调用方（测试直连）的落点
        self.ping_interval = ping_interval
        self._aq: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bind_lock = threading.Lock()
        with _channels_lock:
            _channels.add(self)

    def emit(self, kind: str, payload: Any) -> None:
        """工作线程侧投递（AgentLoop on_event 回调直通）。

        payload：业务事件为 dict；内部信号 `_result` 携带 JobOutcome（编排产出，
        lines() 侧经 build_done_event 转协议）——通道是内部约定信号，不做窄类型。
        """
        item = (kind, payload)
        with self._bind_lock:
            aq, loop = self._aq, self._loop
        if aq is None or loop is None:
            self.queue.put(item)  # 未绑定：落同步缓冲（首次迭代时原子搬运）
            return
        try:
            loop.call_soon_threadsafe(aq.put_nowait, item)
        except RuntimeError:  # 停机竞态：loop 已闭合 → 落缓冲（事件无人消费，无害）
            self.queue.put(item)

    async def lines(self, t0: float,
                    build_done_event: Callable[[Any, int], dict]) -> AsyncIterator[str]:
        """响应侧异步迭代器：done 事件经 build_done_event(result, elapsed_ms) 组装。
        迭代结束（正常收尾/停机 error/客户端断开）即注销——注册表只含活动通道。"""
        aq: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        with self._bind_lock:  # 锁内绑定+搬运：emit 与绑定的竞态在此收口（不重不漏）
            self._aq, self._loop = aq, loop
            while True:
                try:
                    aq.put_nowait(self.queue.get_nowait())
                except Empty:
                    break
        seq = 0

        def next_seq() -> int:
            nonlocal seq
            seq += 1
            return seq

        try:
            while True:
                try:
                    kind, body = await asyncio.wait_for(aq.get(),
                                                        timeout=self.ping_interval)
                except asyncio.TimeoutError:  # ping 心跳（docs/01 §3.4，防反代空闲断连）
                    yield sse_line({"type": "ping", "seq": next_seq()})
                    continue
                if kind == "_result":
                    elapsed_ms = int((time.perf_counter() - t0) * 1000)
                    done = build_done_event(body, elapsed_ms)
                    yield sse_line({"type": "done", "seq": next_seq(), **done})
                    return
                if kind == "_error":
                    yield sse_line({"type": "error", "seq": next_seq(), **body})
                    return
                yield sse_line({"type": kind, "seq": next_seq(), **body})
        finally:
            with _channels_lock:
                _channels.discard(self)
