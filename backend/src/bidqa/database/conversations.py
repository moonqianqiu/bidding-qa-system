"""会话存储（docs/01 §3.1 B1 + B12 控制台旁路）：CRUD + 可见域三元组 + 全库管理。

从 postgres.py 拆出（重构 C2）：可见域语义——viewer=None+machine_domain=True →
仅 NULL（机器域）；viewer=U+True → NULL 或本人（人类 admin）；viewer=U+False →
仅本人（普通用户）；viewer=None+False → 不过滤（进程内历史加载等内部调用）。
控制台全库管理走独立方法（LEFT JOIN users），不改动聊天页可见域三元组。
"""

from __future__ import annotations

import re
from typing import Any

from bidqa.database.connection import PgConn, pg_json

_RE_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def is_uuid(value: str) -> bool:
    return bool(_RE_UUID.match(value or ""))


class ConversationStore:
    """conversations 表访问（持 PgConn 复用连接）。"""

    def __init__(self, conn: PgConn) -> None:
        self._conn = conn

    def connection(self):
        return self._conn.connection()

    @staticmethod
    def _visibility(viewer_user_id: str | None, machine_domain: bool) -> tuple[str, list]:
        """可见域 SQL 片段（模块 docstring 三元组语义）。"""
        if viewer_user_id is None and machine_domain:
            return "user_id IS NULL", []
        if viewer_user_id is None:
            return "", []
        if machine_domain:
            return "(user_id IS NULL OR user_id = %s)", [viewer_user_id]
        return "user_id = %s", [viewer_user_id]

    def upsert_conversation(self, conv_id: str, title: str, messages: dict,
                            provider: str = "", user_id: str | None = None) -> None:
        """归属仅在 INSERT 时写入；ON CONFLICT 的 UPDATE 带 owner 守卫（审计 P1-1
        存储层）：owner 不同的更新整条拒绝（回答/标题/provider 不得写进他人会话）；
        NULL（机器域）行仅机器域可更新（NULL IS NOT DISTINCT FROM NULL=真）。"""
        self.connection().execute(
            "INSERT INTO conversations (id, title, messages, provider, user_id, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, now()) "
            "ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, "
            "messages = EXCLUDED.messages, provider = EXCLUDED.provider, updated_at = now() "
            "WHERE conversations.user_id IS NOT DISTINCT FROM EXCLUDED.user_id",
            (conv_id, title, pg_json(messages), provider, user_id),
        )
        self.connection().commit()

    def get_conversation(self, conv_id: str, viewer_user_id: str | None = None,
                         machine_domain: bool = False) -> dict | None:
        cond, params = self._visibility(viewer_user_id, machine_domain)
        sql = ("SELECT id::text, title, messages, provider, created_at, updated_at "
               "FROM conversations WHERE id = %s")
        if cond:
            sql += f" AND {cond}"
        row = self.connection().execute(sql, (conv_id, *params)).fetchone()
        return dict(row) if row else None

    def list_conversations(self, limit: int = 50, viewer_user_id: str | None = None,
                           machine_domain: bool = False) -> list[dict]:
        cond, params = self._visibility(viewer_user_id, machine_domain)
        sql = ("SELECT id::text, title, provider, created_at, updated_at "
               "FROM conversations")
        if cond:
            sql += f" WHERE {cond}"
        sql += " ORDER BY updated_at DESC LIMIT %s"
        rows = self.connection().execute(sql, (*params, limit)).fetchall()
        return [dict(r) for r in rows]

    def delete_conversation(self, conv_id: str, viewer_user_id: str | None = None,
                            machine_domain: bool = False) -> bool:
        cond, params = self._visibility(viewer_user_id, machine_domain)
        sql = "DELETE FROM conversations WHERE id = %s"
        if cond:
            sql += f" AND {cond}"
        cur = self.connection().execute(sql, (conv_id, *params))
        self.connection().commit()
        return cur.rowcount > 0

    def rename_conversation(self, conv_id: str, title: str,
                            viewer_user_id: str | None = None,
                            machine_domain: bool = False) -> bool:
        cond, params = self._visibility(viewer_user_id, machine_domain)
        sql = "UPDATE conversations SET title = %s, updated_at = now() WHERE id = %s"
        if cond:
            sql += f" AND {cond}"
        cur = self.connection().execute(sql, (title, conv_id, *params))
        self.connection().commit()
        return cur.rowcount > 0

    # ── B12 全库会话管理（admin 控制台旁路；不走可见域三元组）──
    @staticmethod
    def _empty_shell_cond() -> str:
        """空壳判定：items 里没有 assistant 回复（items 缺失一并视为空壳）；
        调用方需再拼 updated_at 保护窗条件（防误删刚流式完成的活跃会话）。"""
        return ("NOT COALESCE(messages->'items' @> "
                "'[{\"role\":\"assistant\"}]'::jsonb, false)")

    def list_all_conversations(self, limit: int = 200, q: str = "") -> list[dict]:
        """控制台全库列表（LEFT JOIN users 取属主名；q 对 title ILIKE 过滤；
        不带 q 过滤时返回全库）。owner 为 NULL = 机器域（user_id 亦为 NULL）。"""
        from bidqa.database.bp_schema import escape_like

        sql = ("SELECT c.id::text AS id, c.title, c.provider, c.updated_at, "
               "c.user_id::text AS user_id, u.username AS owner, "
               "c.messages->'items' AS items, "
               "(messages->'items' @> '[{\"role\":\"assistant\"}]'::jsonb) AS has_reply "
               "FROM conversations c LEFT JOIN users u ON u.id = c.user_id")
        params: list[Any] = []
        if q:
            sql += " WHERE c.title ILIKE %s ESCAPE '\\'"
            params.append(f"%{escape_like(q)}%")
        sql += " ORDER BY c.updated_at DESC LIMIT %s"
        params.append(limit)
        return [dict(r) for r in self.connection().execute(sql, params).fetchall()]

    def empty_conversations(self, protect_minutes: int = 10) -> dict[str, Any]:
        """空壳盘点（purge 预览）：{count, samples(标题×≤20)}；保护窗默认 10 分钟。"""
        cond = (f"{self._empty_shell_cond()} "
                "AND updated_at < now() - make_interval(mins => %s)")
        n = int(self.connection().execute(
            f"SELECT count(*) AS n FROM conversations WHERE {cond}",
            (protect_minutes,)).fetchone()["n"])
        samples = [r["title"] for r in self.connection().execute(
            f"SELECT title FROM conversations WHERE {cond} "
            "ORDER BY updated_at DESC LIMIT 20", (protect_minutes,)).fetchall()]
        return {"count": n, "samples": samples}

    def purge_empty_conversations(self, protect_minutes: int = 10) -> int:
        """删除空壳会话（同盘点条件）；返回删除行数。"""
        cur = self.connection().execute(
            f"DELETE FROM conversations WHERE {self._empty_shell_cond()} "
            "AND updated_at < now() - make_interval(mins => %s)", (protect_minutes,))
        self.connection().commit()
        return max(cur.rowcount, 0)
