"""用户体系认证内核（docs/01 §3.1 用户体系段）：三角色 admin/user/guest。

零新依赖：密码散列用 hashlib.scrypt（stdlib），会话令牌用 secrets.token_urlsafe。
opaque token 存 sessions 表（可吊销、可踢人，比 JWT 诚实）；会话过期在 resolve 时
惰性剔除（顺手清理，无需后台任务）。机器主 token 不经过本模块（中间件直接比对）。
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLE_GUEST = "guest"
MACHINE_USERNAME = "__machine__"  # 机器主 token 的虚拟身份名（无 users 行，user_id=None）


@dataclass
class Identity:
    """请求身份（中间件解析后存 request.state.identity，端点据此做数据隔离）。"""

    role: str  # admin / user / guest
    user_id: str | None  # guest 会话与机器主 token 为 None
    username: str
    token: str = ""  # 原始会话令牌（限流分桶用；机器主 token 不存）


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


class UsersStore:
    """users/sessions/app_settings 三表访问（持 PgClient，复用其连接与 schema）。

    schema 由 build_pg().ensure_schema() 统一执行（schema.sql 单一事实来源）；
    种子由 service.build_users_store 首次构建时调用（进程内一次）。
    """

    def __init__(self, pg) -> None:
        self._pg = pg

    def _conn(self):
        return self._pg.connection()

    # ── 种子 ──
    def seed_if_empty(self, admin_username: str, admin_password: str) -> bool:
        """users 空表则种入首个管理员 + guest 开关默认值；非空即跳过（幂等）。"""
        row = self._conn().execute("SELECT count(*) AS n FROM users").fetchone()
        if row and int(row["n"]) > 0:
            return False
        self._conn().execute(
            "INSERT INTO users (id, username, password_hash, role, disabled) "
            "VALUES (%s, %s, %s, 'admin', false)",
            (str(uuid.uuid4()), admin_username, hash_password(admin_password)))
        self._conn().execute(
            "INSERT INTO app_settings (key, value) VALUES ('guest_enabled', 'true') "
            "ON CONFLICT (key) DO NOTHING")
        self._conn().commit()
        return True

    # ── 登录 ──
    def login(self, username: str, password: str, ttl_hours: int) -> dict | None:
        """凭据校验 → 发会话；用户不存在/禁用/密码错统一 None（不区分，防枚举）。"""
        row = self._conn().execute(
            "SELECT id, username, password_hash, role, disabled FROM users "
            "WHERE username = %s", (username,)).fetchone()
        if not row or row["disabled"] or not verify_password(password, row["password_hash"]):
            return None
        return self._mint_session(row["id"], row["username"], row["role"], ttl_hours)

    def login_guest(self, ttl_hours: int) -> dict | None:
        """游客会话（无 users 行：user_id=NULL role=guest）；开关关闭=None。"""
        if not self.guest_enabled():
            return None
        return self._mint_session(None, ROLE_GUEST, ROLE_GUEST, ttl_hours)

    def _mint_session(self, user_id, username, role, ttl_hours) -> dict:
        token = mint_token()
        self._conn().execute(
            "INSERT INTO sessions (token, user_id, role, created_at, expires_at) "
            "VALUES (%s, %s, %s, now(), now() + make_interval(hours => %s))",
            (token, user_id, role, int(ttl_hours)))
        self._conn().execute("DELETE FROM sessions WHERE expires_at < now()")  # 顺手清理过期
        self._conn().commit()
        return {"token": token, "username": username, "role": role}

    def resolve(self, token: str) -> Identity | None:
        """token → 身份；过期（惰性删除）或所属用户被禁用 → None。"""
        row = self._conn().execute(
            "SELECT s.role, s.user_id, s.expires_at, u.username, u.disabled "
            "FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
            "WHERE s.token = %s", (token,)).fetchone()
        if not row:
            return None
        expires = row.get("expires_at")
        if expires is not None:
            exp = expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)
            if exp < datetime.now(timezone.utc):
                self._conn().execute("DELETE FROM sessions WHERE token = %s", (token,))
                self._conn().commit()
                return None
        if row.get("disabled"):
            return None
        user_id = row.get("user_id")
        return Identity(role=row["role"],
                        user_id=str(user_id) if user_id else None,
                        username=row.get("username") or ROLE_GUEST, token=token)

    def logout(self, token: str) -> bool:
        cur = self._conn().execute("DELETE FROM sessions WHERE token = %s", (token,))
        self._conn().commit()
        return cur.rowcount > 0

    # ── 管理动作（CLI 本轮；控制台界面下轮）──
    def create_user(self, username: str, password: str, role: str) -> str | None:
        """重名返回 None（调用方报错）；role 仅 admin/user。"""
        dup = self._conn().execute(
            "SELECT 1 FROM users WHERE username = %s", (username,)).fetchone()
        if dup:
            return None
        uid = str(uuid.uuid4())
        self._conn().execute(
            "INSERT INTO users (id, username, password_hash, role, disabled) "
            "VALUES (%s, %s, %s, %s, false)",
            (uid, username, hash_password(password), role))
        self._conn().commit()
        return uid

    def reset_password(self, username: str, password: str) -> int | None:
        """改密并吊销该用户全部会话（单一入口：CLI `user reset` 与控制台 API 同款）。

        返回吊销的会话数；用户不存在 → None。原子（改密+吊销同事务）——半途失败
        不能出现「密码已改但旧会话仍有效」的窗口。"""
        conn = self._conn()
        with conn.transaction():
            cur = conn.execute(
                "UPDATE users SET password_hash = %s WHERE username = %s",
                (hash_password(password), username))
            if cur.rowcount == 0:
                return None
            rev = conn.execute(
                "DELETE FROM sessions WHERE user_id = "
                "(SELECT id FROM users WHERE username = %s)", (username,))
            revoked = max(rev.rowcount, 0)
        return revoked

    def set_disabled(self, username: str, disabled: bool) -> bool:
        cur = self._conn().execute(
            "UPDATE users SET disabled = %s WHERE username = %s", (disabled, username))
        self._conn().commit()
        return cur.rowcount > 0

    def list_users(self) -> list[dict]:
        """用户列表 + 每人活跃会话数（LEFT JOIN 未过期会话；控制台用户表用，CLI 兼容）。"""
        rows = self._conn().execute(
            "SELECT u.username, u.role, u.disabled, u.created_at, "
            "count(s.token)::int AS sessions "
            "FROM users u LEFT JOIN sessions s "
            "ON s.user_id = u.id AND s.expires_at > now() "
            "GROUP BY u.username, u.role, u.disabled, u.created_at "
            "ORDER BY u.created_at").fetchall()
        return [dict(r) for r in rows]

    def count_users(self) -> int:
        row = self._conn().execute("SELECT count(*) AS n FROM users").fetchone()
        return int(row["n"]) if row else 0

    # ── 运行时开关（app_settings）──
    def guest_enabled(self) -> bool:
        """无记录=默认开（种子前与旧行为一致）；显式 'false' 才算关。"""
        row = self._conn().execute(
            "SELECT value FROM app_settings WHERE key = 'guest_enabled'").fetchone()
        return row is None or row["value"] == "true"

    def set_guest_enabled(self, enabled: bool) -> None:
        self._conn().execute(
            "INSERT INTO app_settings (key, value) VALUES ('guest_enabled', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            ("true" if enabled else "false",))
        self._conn().commit()
