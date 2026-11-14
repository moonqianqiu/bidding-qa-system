"""prune 子命令（H7 数据保留清理）：conversations/feedback 两表原子清理。

SQL 保持在此（两行 DELETE，无业务规则可下沉）；原子性靠显式事务
（autocommit 连接下 conn.transaction()，docs/01 §4.3 约定）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from bidqa.database.factory import build_pg


def cmd_prune(args, _settings: Any) -> int:
    pg = build_pg()
    if pg is None:
        print("PostgreSQL 未配置/不可用", file=__import__("sys").stderr)
        return 2
    cutoff = datetime.now() - timedelta(days=args.days)
    conn = pg.connection()
    conv = conn.execute(
        "SELECT count(*) AS c FROM conversations WHERE updated_at < %s", (cutoff,)).fetchone()
    fb = conn.execute(
        "SELECT count(*) AS c FROM feedback WHERE created_at < %s", (cutoff,)).fetchone()
    print(f"将清理 {args.days} 天前的会话 {conv['c']} 条、反馈 {fb['c']} 条")
    if args.dry_run:
        print("试运行结束，未删除")
        return 0
    with conn.transaction():  # 两表清理原子性（autocommit 连接下显式包）
        conn.execute("DELETE FROM conversations WHERE updated_at < %s", (cutoff,))
        conn.execute("DELETE FROM feedback WHERE created_at < %s", (cutoff,))
    print(f"已清理（conversations {conv['c']}，feedback {fb['c']}）")
    return 0
