"""幂等前置守卫（docs/01 §3.3）：client_msg_id 60s 去重窗口 → 4091。

从 chat_stream 归位（重构 C3）：纯 dict 逻辑独立成模块（单测锁定语义）；
进程内有界防泄漏，单进程约定（docs/01 §6，workers=1）。
审计 P2-33：锁内 check-and-set（原无锁——并发同键双跑=重复调用收费模型）+
LRU 淘汰（原插入序——误删仍在窗口内的活跃键）+ owner 分域（原全局键——
不同用户撞同一 client_msg_id 误 4091）。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

IDEMPOTENCY_WINDOW = 60.0
_CLIENT_MSG_MAX = 512  # 有界防泄漏（超限淘汰最旧一半，LRU 口径）

_client_msg_seen: OrderedDict[str, float] = OrderedDict()  # owner|client_msg_id -> monotonic ts
_seen_lock = threading.Lock()


def check_client_msg_id(client_msg_id: str | None, owner: str = "") -> dict | None:
    """窗口内重复提交 → 冲突错误体（调用方转 409 响应）；新键登记并返回 None。"""
    if not client_msg_id:
        return None
    key = f"{owner}|{client_msg_id}"
    now = time.monotonic()
    with _seen_lock:
        seen_at = _client_msg_seen.get(key)
        if seen_at is not None and now - seen_at < IDEMPOTENCY_WINDOW:
            _client_msg_seen.move_to_end(key)  # LRU：命中即刷新（防被淘汰误删活跃键）
            return {"error_code": 4091, "message": "重复提交（60s 内相同 client_msg_id）",
                    "detail": {"client_msg_id": client_msg_id}}
        _client_msg_seen[key] = now
        _client_msg_seen.move_to_end(key)
        if len(_client_msg_seen) > _CLIENT_MSG_MAX:  # 有界防泄漏（LRU 淘汰最旧一半）
            for stale in list(_client_msg_seen)[:_CLIENT_MSG_MAX // 2]:
                _client_msg_seen.pop(stale, None)
    return None


def clear_seen() -> None:
    """测试隔离用：清空幂等窗口。"""
    _client_msg_seen.clear()
