"""认证内核单元（docs/01 §3.1）：scrypt 散列/令牌/Identity；UsersStore 的 PG 集成
（种子幂等/登录/过期/吊销）在 db_test/test_postgres_integration.py。零依赖纯 stdlib。"""

from __future__ import annotations

from bidqa.auth_core import (
    MACHINE_USERNAME,
    ROLE_ADMIN,
    ROLE_GUEST,
    ROLE_USER,
    Identity,
    hash_password,
    mint_token,
    verify_password,
)


def test_password_hash_roundtrip() -> None:
    stored = hash_password("s3cret-口令")
    assert stored.startswith("scrypt$")  # 自带参数前缀，verify 自解析
    assert verify_password("s3cret-口令", stored)
    assert not verify_password("wrong", stored)


def test_password_salt_unique_per_call() -> None:
    """同口令两次散列产生不同盐（防彩虹表），但都能通过验证。"""
    a, b = hash_password("same"), hash_password("same")
    assert a != b
    assert verify_password("same", a) and verify_password("same", b)


def test_verify_malformed_stored_is_false() -> None:
    """格式不认识/字段缺失一律 False，绝不抛错（防脏数据打穿登录）。"""
    for stored in ("", "plain", "bcrypt$1$2$3", "scrypt$x$8$1$zz$ff", "scrypt$16384$8$1"):
        assert verify_password("pw", stored) is False


def test_mint_token_high_entropy() -> None:
    tokens = {mint_token() for _ in range(50)}
    assert len(tokens) == 50  # 无碰撞
    assert all(len(t) >= 32 for t in tokens)


def test_identity_defaults_and_roles() -> None:
    ident = Identity(ROLE_ADMIN, None, MACHINE_USERNAME)
    assert ident.token == ""  # 机器主 token 不存原文
    assert Identity(ROLE_GUEST, None, ROLE_GUEST, "t").user_id is None
    assert Identity(ROLE_USER, "u-1", "alice", "t").role == ROLE_USER
