"""口令散列与会话令牌（docs/01 §3.1 用户体系段）：零新依赖，纯 stdlib。

密码散列用 hashlib.scrypt（格式串自带参数，verify 自解析）；会话令牌用
secrets.token_urlsafe（opaque token 存 sessions 表——可吊销、可踢人，比 JWT 诚实）。
机器主 token 不经过本模块（中间件直接比对）。
"""

from __future__ import annotations

import hashlib
import secrets


def hash_password(password: str) -> str:
    """scrypt 散列，格式 scrypt$n$r$p$salt_hex$dk_hex（自带参数，verify 自解析）。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${2**14}$8$1${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """常数时间比对；格式不认识/参数异常一律 False（不抛错）。"""
    try:
        scheme, n, r, p, salt_hex, dk_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p), dklen=32)
        return secrets.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def mint_token() -> str:
    return secrets.token_urlsafe(32)
