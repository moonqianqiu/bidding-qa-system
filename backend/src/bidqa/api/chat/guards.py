"""幂等前置守卫（docs/01 §3.3）：client_msg_id 60s 去重窗口 → 4091。

从 chat_stream 归位（重构 C3）：纯 dict 逻辑独立成模块（单测锁定语义）；
进程内有界防泄漏，单进程约定（docs/01 §6，workers=1）。
"""

from __future__ import annotations

import time

IDEMPOTENCY_WINDOW = 60.0

_client_msg_seen: dict[str, float] = {}  # client_msg_id -> monotonic ts（单进程）


def check_client_msg_id(client_msg_id: str | None) -> dict | None:
    """窗口内重复提交 → 冲突错误体（调用方转 409 响应）；新键登记并返回 None。

    有界防泄漏：超过 512 键时丢最旧一半（先到先留，与窗口语义一致）。
    """
    if not client_msg_id:
        return None
    now = time.monotonic()
    seen_at = _client_msg_seen.get(client_msg_id)
    if seen_at is not None and now - seen_at < IDEMPOTENCY_WINDOW:
        return {"error_code": 4091, "message": "重复提交（60s 内相同 client_msg_id）",
                "detail": {"client_msg_id": client_msg_id}}
    _client_msg_seen[client_msg_id] = now
    if len(_client_msg_seen) > 512:  # 有界防泄漏
        for key in list(_client_msg_seen)[:256]:
            _client_msg_seen.pop(key, None)
    return None


def clear_seen() -> None:
    """测试隔离用：清空幂等窗口。"""
    _client_msg_seen.clear()
