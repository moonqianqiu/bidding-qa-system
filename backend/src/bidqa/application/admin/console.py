"""控制台聚合用例（docs/01 §3.1 A15）：三库规模一端点取齐（KPI 仪表盘数据面）。

诚实降级（跨层硬约束 1）：任一库挂 → 该节 {"status": "不可用"}，其余节照常，
绝不记 0、不整页报废。工厂直接取用窄 factory（database/auth），不经过组合根。
30s 缓存属端点层（api/system/console），不在这里。
"""

from __future__ import annotations

from typing import Any

from bidqa.auth.factory import build_users_store
from bidqa.config import Settings
from bidqa.database.factory import build_neo, build_pg, build_repo

_UNAVAILABLE = {"status": "不可用"}  # 硬约束 1：取不到记「不可用」，绝不记 0


def collect_pg_counts() -> dict[str, Any]:
    try:
        pg = build_pg()
        if pg is None:
            raise RuntimeError("未配置或不可用")
        conn = pg.connection()
        out: dict[str, Any] = {
            table: int(conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"])
            for table in ("bidding_procurement", "conversations", "feedback")}
        store = build_users_store()
        out["users"] = store.count_users() if store else "不可用"
        return out
    except Exception:
        return dict(_UNAVAILABLE)


def collect_qdrant_counts(collection: str) -> dict[str, Any]:
    try:
        repo = build_repo()
        return {"vectors": int(repo.count()), "collection": collection}
    except Exception:
        return dict(_UNAVAILABLE)


def collect_neo_counts() -> dict[str, Any]:
    try:
        neo = build_neo()
        if neo is None:
            raise RuntimeError("未配置或不可用")
        labels = neo.counts()
        return {"labels": labels, "total": sum(int(v) for v in labels.values())}
    except Exception:
        return dict(_UNAVAILABLE)


def collect_all(settings: Settings) -> dict[str, Any]:
    """三节聚合（pg/qdrant/neo4j）；调用方负责 30s 缓存。settings 经 DI 注入。"""
    return {"pg": collect_pg_counts(),
            "qdrant": collect_qdrant_counts(settings.QDRANT_COLLECTION),
            "neo4j": collect_neo_counts()}
