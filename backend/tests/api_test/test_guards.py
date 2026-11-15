"""幂等守卫单测（审计 P2-33）：锁 / LRU 淘汰 / 用户分域。"""

from __future__ import annotations

import threading

from bidqa.api.chat import guards as guards_mod
from bidqa.api.chat.guards import check_client_msg_id, clear_seen


def setup_function() -> None:
    clear_seen()


def teardown_function() -> None:
    clear_seen()


def test_concurrent_same_key_single_winner() -> None:
    """P2-33①（审计）：check-and-set 无锁——并发同键可双跑（重复调用收费模型）。
    加锁后并发提交同一键：恰好 1 个注册成功，其余 4091。"""
    barrier = threading.Barrier(8)
    outcomes: list[dict | None] = []

    def submit() -> None:
        barrier.wait()
        outcomes.append(check_client_msg_id("race-key"))

    ts = [threading.Thread(target=submit, daemon=True) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert outcomes.count(None) == 1  # 恰一个赢家
    assert len([o for o in outcomes if o and o["error_code"] == 4091]) == 7


def test_eviction_is_lru_not_insertion_order() -> None:
    """P2-33②（审计）：超 512 键曾按插入序淘汰——会误删仍在 60s 窗口内的活跃键。
    收口后按最后命中时间（LRU）：刚被再次命中的 k0000 必须存活（仍在窗口=4091）。"""
    for i in range(512):
        check_client_msg_id(f"k{i:04d}")
    check_client_msg_id("k0000")  # 活跃键：最后命中（LRU 应保它，插入序淘汰会删它）
    check_client_msg_id("overflow")  # 第 513 个 → 触发淘汰一半
    conflict = check_client_msg_id("k0000")
    assert conflict is not None and conflict["error_code"] == 4091  # 旧插入序淘汰=k0000 已删→None（红）


def test_keys_scoped_by_owner() -> None:
    """P2-33③（审计）：键不分用户域——不同用户撞同一 client_msg_id 误 4091。
    分域后各自独立窗口。"""
    assert check_client_msg_id("same-key", owner="user-a") is None
    conflict = check_client_msg_id("same-key", owner="user-b")
    assert conflict is None  # 他人窗口的键与我无关（原全局键=红）
    assert check_client_msg_id("same-key", owner="user-a") is not None  # 同域仍 4091
