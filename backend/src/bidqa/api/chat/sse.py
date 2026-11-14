"""SSE 传输机制（docs/01 §3.4）：事件队列 + ping 心跳 + seq 单调 + data: JSON 帧。

从 chat_stream 归位（重构 C3）：纯机制、零业务——工作线程经 emit 投递事件，
generate 迭代器按「事件即下发、空闲 15s 发 ping」泵出。契约序
（status*→thinking*→token*→done|error）由 AgentLoop/worker 保证，本层不重排。
错误体（_error）与收尾（_result）是 worker→传输的内部约定信号。
"""

from __future__ import annotations

import json
import time
from queue import Empty, Queue
from typing import Any, Callable, Iterator

PING_INTERVAL = 15.0  # 心跳间隔（防反向代理空闲断连，Nginx 默认 60s）


def sse_line(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


class SseChannel:
    """工作线程 → StreamingResponse 的事件通道（单连接生命周期）。"""

    def __init__(self, ping_interval: float = PING_INTERVAL):
        self.queue: Queue = Queue()
        self.ping_interval = ping_interval

    def emit(self, kind: str, payload: Any) -> None:
        """工作线程侧投递（AgentLoop on_event 回调直通）。

        payload：业务事件为 dict；内部信号 `_result` 携带 JobOutcome（编排产出，
        lines() 侧经 build_done_event 转协议）——通道是内部约定信号，不做窄类型。
        """
        self.queue.put((kind, payload))

    def lines(self, t0: float,
              build_done_event: Callable[[Any, int], dict]) -> Iterator[str]:
        """响应侧迭代器：done 事件经 build_done_event(result, elapsed_ms) 组装。"""
        seq = 0

        def next_seq() -> int:
            nonlocal seq
            seq += 1
            return seq

        while True:
            try:
                kind, body = self.queue.get(timeout=self.ping_interval)
            except Empty:  # ping 心跳（docs/01 §3.4，防反代空闲断连）
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
