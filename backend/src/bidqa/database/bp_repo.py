"""D3 业务库读写（docs/02 D3；docs/01 §4.3 F6 判重语义）。

从 postgres.py 拆出（重构 C2）：持 PgConn 复用连接；六类查询走 bp_query 纯函数，
detail_search 正向落空且关键词 ≥3 字时自动反向匹配兜底（关键词包含字段值，
解决 LLM 传参是实际名称超集的问题）；filters/date_range 在反向变体里原样保留。
"""

from __future__ import annotations

from typing import Any

from bidqa.database.bp_query import build_bp_query
from bidqa.database.bp_schema import BP_COLUMNS
from bidqa.database.connection import PgConn


class BpRepo:
    """业务明细表读写（upsert 判重：同批次=更新，跨批次=并存，docs/01 §4.3）。"""

    def __init__(self, conn: PgConn) -> None:
        self._conn = conn

    def connection(self):
        return self._conn.connection()

    def query_bp(self, args: dict[str, Any]) -> tuple[list[dict], str]:
        """返回 (rows, sql)；SQL 构建异常原样抛 ValueError（工具层转坏参数）。"""
        sql, params = build_bp_query(args)
        rows = self.connection().execute(sql, params).fetchall()
        keyword = str(args.get("keyword") or "").strip()
        if (not rows and args.get("query_type") == "detail_search"
                and 3 <= len(keyword) <= 200):
            rsql, rparams = build_bp_query(args, reverse=True)
            rows = self.connection().execute(rsql, rparams).fetchall()
            if rows:
                sql = rsql
        return rows, sql

    def upsert_bp(self, rows: list[dict[str, Any]], batch_id: str) -> int:
        """F6 upsert（同批次=更新，跨批次=并存）；批量导入原子（任一行失败整批回滚）。"""
        cols = [c for c in BP_COLUMNS if c != "batch_id"]
        placeholders = ", ".join("%s" for _ in cols)
        updates = ", ".join(
            f"{c} = EXCLUDED.{c}" for c in cols if c not in ("project_no", "batch_id"))
        updates += ", updated_at = now()"
        sql = (
            f"INSERT INTO bidding_procurement ({', '.join(cols)}, batch_id) "
            f"VALUES ({placeholders}, %s) "
            f"ON CONFLICT (project_no, batch_id) DO UPDATE SET {updates}"
        )
        conn = self.connection()
        count = 0
        with conn.transaction():  # autocommit 连接下显式包事务：批量原子
            for row in rows:
                values = [row.get(c) for c in cols] + [batch_id]
                conn.execute(sql, values)
                count += 1
        return count

    def update_subject_matter(self, mapping: dict[str, str],
                              batch_id: str | None = None) -> int:
        """按 project_name 批量写回标的物（subject-batch fetch --write）；返回更新行数。

        P1-16：可指定 batch_id 收窄到单批次——缺省仍按名称全表写回（旧语义），
        跨批次同号项目会被一起改，调用方需要精确范围时显式传批次。
        """
        conn = self.connection()
        where = "project_name = %s" + (" AND batch_id = %s" if batch_id else "")
        updated = 0
        with conn.transaction():  # 批量写回原子性（autocommit 下显式包）
            for name, sm in mapping.items():
                params = (sm, name, batch_id) if batch_id else (sm, name)
                cur = conn.execute(
                    f"UPDATE bidding_procurement SET subject_matter = %s, updated_at = now() "
                    f"WHERE {where}",
                    params)
                updated += max(cur.rowcount, 0)
        return updated
