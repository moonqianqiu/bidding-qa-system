"""反馈存储（docs/01 §3.1 A7 反馈闭环）：点赞/点踩落库，点踩触发在线判分。

从 postgres.py 拆出（重构 C2）：单一职责小模块——语义与写入路径一目了然。
"""

from __future__ import annotations

from bidqa.database.connection import PgConn, pg_json


class FeedbackStore:
    """feedback 表访问（持 PgConn 复用连接）。"""

    def __init__(self, conn: PgConn) -> None:
        self._conn = conn

    def connection(self):
        return self._conn.connection()

    def insert_feedback(self, conversation_id: str | None, question: str, rating: str,
                        sources: list, trace_id: str = "") -> int:
        if rating not in ("up", "down"):
            raise ValueError("rating 须为 up|down")
        row = self.connection().execute(
            "INSERT INTO feedback (conversation_id, question, rating, sources, trace_id) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (conversation_id, question, rating, pg_json(sources), trace_id)).fetchone()
        self.connection().commit()
        return int(row["id"])

    def ratings_for_conversation(self, conversation_id: str) -> list[dict]:
        """会话内反馈态回查（A7 刷新还原）：同问题取最新一条（id DESC）。
        显式 dict_row 游标：连接默认工厂类型检查器推不出（dict_row 在 connect 时注入）。"""
        from psycopg.rows import dict_row

        with self.connection().cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                "SELECT DISTINCT ON (question) question, rating FROM feedback "
                "WHERE conversation_id = %s ORDER BY question, id DESC",
                (conversation_id,)).fetchall()
        return [{"question": r["question"], "rating": r["rating"]} for r in rows]
