"""B4 限流（docs/01 §3.5）：滑动窗口 30 次/60s，仅问答类端点；游客独立更严配额。

身份：用户体系 armed=按角色分桶（游客独立 limiter 实例，配额 GUEST_RATE_LIMIT；
普通/管理员=主桶按 user_id）；legacy 开发模式=Bearer token / 客户端 IP。
进程内实现（单进程约定）；429 必带 Retry-After 头；被限流请求不计入在线评测
（由调用方在限流拒绝后直接返回，不进入评测埋点）。进程内 _hits 是唯一状态：
闲置客户端条目每 CLEANUP_INTERVAL 批量清理一次（只丢最新命中已滑出窗口的条目，
配额语义不变），长跑防膨胀。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import Request

from bidqa.auth.identity import ROLE_GUEST

WINDOW_SECONDS = 60.0
MAX_REQUESTS = 30
CLEANUP_INTERVAL = 300.0

RATE_LIMITED_PATHS = ("/api/chat/stream", "/api/ask", "/api/vision")


class SlidingWindowLimiter:
    def __init__(self, max_requests: int = MAX_REQUESTS,
                 window_seconds: float = WINDOW_SECONDS):
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._last_cleanup = 0.0  # 0.0 = 首次 check 即做一次（空表，代价为零）
        self._lock = threading.Lock()

    def check(self, identity: str) -> tuple[bool, int]:
        """返回 (是否放行, retry_after 秒)。滑动窗口：清掉窗口外命中再判。"""
        now = time.monotonic()
        with self._lock:
            if now - self._last_cleanup >= CLEANUP_INTERVAL:
                self._sweep(now)
                self._last_cleanup = now
            hits = self._hits[identity]
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.max_requests:
                retry_after = int(self.window - (now - hits[0])) + 1
                return False, max(retry_after, 1)
            hits.append(now)
            return True, 0

    def _sweep(self, now: float) -> None:
        """清理「无活跃请求」的客户端条目（须持锁调用）：最新命中滑出窗口即整条丢弃，
        该客户端下次请求经 defaultdict 重建，配额语义与逐条 popleft 完全一致。"""
        stale = [k for k, hits in self._hits.items()
                 if not hits or now - hits[-1] >= self.window]
        for key in stale:
            self._hits.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_limiter = SlidingWindowLimiter()
_guest_limiter: SlidingWindowLimiter | None = None  # 游客独立桶（配额变更时重建）
_guest_limit_at: int = -1
_guest_lock = threading.Lock()


def get_limiter() -> SlidingWindowLimiter:
    return _limiter


def get_guest_limiter(max_requests: int) -> SlidingWindowLimiter:
    """游客桶：按 GUEST_RATE_LIMIT 惰性构建，配额变更（改配置/测试）即重建。
    双检锁（审计 P2-34：判空→new→赋值无锁——冷启动并发可建多桶，配额被放大）。"""
    global _guest_limiter, _guest_limit_at
    if _guest_limiter is None or _guest_limit_at != max_requests:
        with _guest_lock:
            if _guest_limiter is None or _guest_limit_at != max_requests:
                _guest_limiter = SlidingWindowLimiter(max_requests=max_requests)
                _guest_limit_at = max_requests
    return _guest_limiter


def identity_of(request: Request, auth_token_configured: bool) -> str:
    """配置鉴权=Bearer token；开发模式=客户端 IP（docs/01 §3.5；legacy 路径）。"""
    if auth_token_configured:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return f"token:{auth[7:].strip()}"
    client = request.client.host if request.client else "unknown"
    return f"ip:{client}"


def rate_identity(request: Request, settings) -> tuple[SlidingWindowLimiter, str]:
    """按请求身份选限流桶（docs/01 §3.5 游客配额）：
    游客=独立桶按会话 token；普通/管理员=主桶按 user_id（机器主 token 同桶）；
    无身份（legacy 开发模式）=主桶走旧 identity_of。返回 (limiter, identity)。"""
    identity = getattr(request.state, "identity", None)
    if identity is None:
        return get_limiter(), identity_of(request, settings.auth_enabled)
    if identity.role == ROLE_GUEST:
        return get_guest_limiter(int(getattr(settings, "GUEST_RATE_LIMIT", 10) or 10)), \
            f"guest:{identity.token}"
    return get_limiter(), f"user:{identity.user_id or identity.username}"
