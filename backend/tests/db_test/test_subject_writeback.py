"""标的物写回集成测试（真实 PG；服务不可用自动跳过，CI 兼容）。

红线（AGENTS.md「测试不得污染真实仓库」）：it-sm 批次数据收尾必删
（try/finally）；updated==3 断言兼作同名项目残留金丝雀。
2026-11-08 之前本测试收尾不清理，曾在开发库留下 3 行 it-sm 残留（+3 行之谜）。
"""

from __future__ import annotations

from datetime import date

from bidqa.database.bp_repo import BpRepo
from bidqa.database.connection import PgConn

ROWS = [
    {"project_no": "IT-SM-001", "project_name": "空调采购项目", "winner": "甲公司",
     "award_amount": 660_000, "region": "安徽", "publish_date": date(2025, 1, 15)},
    {"project_no": "IT-SM-002", "project_name": "空调采购项目", "winner": "乙公司",
     "award_amount": 720_000, "region": "合肥", "publish_date": date(2025, 2, 1)},
    {"project_no": "IT-SM-003", "project_name": "电梯采购项目", "winner": "丙公司",
     "award_amount": 900_000, "region": "芜湖", "publish_date": date(2025, 3, 1)},
]


def _cleanup(pg: PgConn) -> None:
    pg.connection().execute("DELETE FROM bidding_procurement WHERE batch_id = 'it-sm'")
    pg.connection().commit()


def test_update_subject_matter_writes_same_name_rows(pg_client: PgConn) -> None:
    pg = pg_client
    bp = BpRepo(pg)
    _cleanup(pg)  # 前置：清历史残留
    try:
        bp.upsert_bp([dict(r, batch_id="it-sm") for r in ROWS], "it-sm")
        updated = bp.update_subject_matter({"空调采购项目": "空调", "电梯采购项目": "电梯"})
        rows = pg.connection().execute(
            "SELECT project_no, subject_matter FROM bidding_procurement "
            "WHERE batch_id = 'it-sm' ORDER BY project_no").fetchall()
        assert updated == 3  # 同名两行都更新
        assert {r["project_no"]: r["subject_matter"] for r in rows} == {
            "IT-SM-001": "空调", "IT-SM-002": "空调", "IT-SM-003": "电梯"}
        # 未知项目名不炸（rowcount=0），幂等重跑行数一致
        assert bp.update_subject_matter({"不存在的项目": "x"}) == 0
        assert bp.update_subject_matter({"空调采购项目": "空调"}) == 2
    finally:
        _cleanup(pg)  # 收尾：不污染开发库
