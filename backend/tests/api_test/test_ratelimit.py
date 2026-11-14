"""B4 限流单元：滑动窗口语义 + 闲置客户端条目清理（docs/01 §3.5）。

清理间隔 300s 用假时钟驱动（monkeypatch time.monotonic）——真实间隔不能等；
_first cleanup 锚点从 0.0 起，首次 check 即完成一次空清扫并落锚，行为确定。
"""

from __future__ import annotations

from bidqa.api import ratelimit
from bidqa.api.ratelimit import CLEANUP_INTERVAL, SlidingWindowLimiter


def _patch_clock(monkeypatch, start: float) -> dict:
    clock = {"t": start}
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: clock["t"])
    return clock


def test_first_check_triggers_initial_sweep(monkeypatch) -> None:
    """首次 check 做一次空清扫并落锚清理时间，不影响放行。"""
    clock = _patch_clock(monkeypatch, 1000.0)
    lim = SlidingWindowLimiter()
    assert lim.check("a") == (True, 0)
    assert lim._last_cleanup == clock["t"]


def test_stale_client_swept_after_interval(monkeypatch) -> None:
    """闲置客户端条目在清理点整批丢弃，其后续请求重新从头计数。"""
    clock = _patch_clock(monkeypatch, 1000.0)
    lim = SlidingWindowLimiter(max_requests=2)
    assert lim.check("a") == (True, 0)
    assert lim.check("a") == (True, 0)
    assert lim.check("a")[0] is False  # 满额拒绝（拒绝不追加时间戳）
    clock["t"] += CLEANUP_INTERVAL + 1
    assert lim.check("b") == (True, 0)  # 触发清理的这次自身正常放行
    assert "a" not in lim._hits
    assert "b" in lim._hits
    assert lim.check("a") == (True, 0)  # a 经 defaultdict 重建


def test_active_client_survives_cleanup(monkeypatch) -> None:
    """清理只丢「最新命中已滑出窗口」的条目，活跃客户端保留。"""
    clock = _patch_clock(monkeypatch, 1000.0)
    lim = SlidingWindowLimiter(max_requests=5)
    assert lim.check("cold") == (True, 0)
    clock["t"] += CLEANUP_INTERVAL  # t=1300：cold 早已滑出窗口
    assert lim.check("hot") == (True, 0)
    clock["t"] += 1  # t=1301：距上次清理锚点 301s，本次触发清理
    assert lim.check("third") == (True, 0)
    assert "cold" not in lim._hits
    assert "hot" in lim._hits  # 最新命中距今 1s < 60
    assert "third" in lim._hits


def test_sweep_preserves_window_semantics(monkeypatch) -> None:
    """被清条目的旧命中本已全部滑出窗口——清理不改变任何配额判定结果。"""
    clock = _patch_clock(monkeypatch, 1000.0)
    lim = SlidingWindowLimiter(max_requests=3)
    for _ in range(3):
        assert lim.check("a") == (True, 0)
    assert lim.check("a")[0] is False
    clock["t"] += CLEANUP_INTERVAL + 1  # t=1301：触发清理
    assert lim.check("a")[0] is True  # 旧命中全滑出，清理与否结论相同
    assert sum(1 for _ in lim._hits["a"]) == 1


# ── 用户体系（docs/01 §3.5）：按身份分桶，游客独立更严配额 ──
def _req_with_identity(ident):
    from types import SimpleNamespace
    return SimpleNamespace(state=SimpleNamespace(identity=ident), client=None)


def test_rate_identity_selects_buckets():
    """游客→独立桶（按会话 token）；user/admin→主桶（按 user_id）；无身份→旧 identity_of。"""
    from bidqa.auth.identity import Identity
    from bidqa.config import Settings

    s = Settings(_env_file=None, GUEST_RATE_LIMIT=2, USERS_ENABLED="true")
    from bidqa.api.ratelimit import get_guest_limiter, get_limiter, rate_identity

    lim, bucket = rate_identity(
        _req_with_identity(Identity("guest", None, "guest", "tok-1")), s)
    assert lim is get_guest_limiter(2) and bucket == "guest:tok-1"
    lim2, bucket2 = rate_identity(
        _req_with_identity(Identity("user", "u-9", "alice", "t")), s)
    assert lim2 is get_limiter() and bucket2 == "user:u-9"
    machine = rate_identity(
        _req_with_identity(Identity("admin", None, "__machine__", "")), s)
    assert machine[0] is get_limiter() and machine[1] == "user:__machine__"


def test_guest_bucket_independent_of_main():
    """游客第 3 次请求拒绝（配额 2），主桶不受影响（docs/01 §3.5 游客独立更严配额）。"""
    from bidqa.auth.identity import Identity
    from bidqa.config import Settings

    from bidqa.api.ratelimit import rate_identity

    s = Settings(_env_file=None, GUEST_RATE_LIMIT=2)
    glim, gbucket = rate_identity(
        _req_with_identity(Identity("guest", None, "guest", "tok-1")), s)
    ulim, ubucket = rate_identity(
        _req_with_identity(Identity("user", "u-9", "alice", "t")), s)
    assert glim is not ulim  # 两只桶
    assert [glim.check(gbucket)[0] for _ in range(2)] == [True, True]
    assert glim.check(gbucket)[0] is False
    assert ulim.check(ubucket) == (True, 0)  # 主桶不受游客限流影响
