"""done 事件组装（docs/01 §3.4 B2）：LoopResult → done dict，HTTP 无关。

从 api/chat/stream 归位（v2.2）：事件形态属聊天编排契约，api 只负责把它
交给 SseChannel——api 不再直接 import agent 层（分层 R11）。
"""

from __future__ import annotations

from typing import Any

from bidqa.agent.assembly import build_done


def build_done_event(result: Any, *, elapsed_ms: int, trace_id: str,
                     provider: str) -> dict:
    return build_done(result, elapsed_ms=elapsed_ms, trace_id=trace_id,
                      provider=provider)
