"""工具描述契约：描述承诺必须与底层实现逐条对得上（82cc77f 教训的反向钉死）。

ag-010 误澄清修复配套：「按中标总额看排名靠前的供应商是谁」这类排名/TopN 题由
search_database / search_graph 直接承接（描述声明 无限定条件=全库口径、按累计
中标总额降序、默认 Top 10）——把这三条承诺分别钉到 SQL / Cypher 实现，
防描述强化后底层漂移（描述与实现不一致 = 新的误澄清/误承诺来源）。
"""

from __future__ import annotations

from bidqa.database.bp_query import build_bp_query
from bidqa.database.neo4j import NeoClient
from bidqa.tools.db_tool import SearchDatabaseTool
from bidqa.tools.graph_tool import SearchGraphTool

# ── 描述侧：承接口径在位（两工具分工表述一致）──


def test_db_description_claims_topn_by_total_amount() -> None:
    desc = SearchDatabaseTool.description
    assert "supplier_topn" in desc  # 问题→参数映射显式可查
    assert "中标总额" in desc  # 排名口径=累计中标总额（非项目数）
    assert "无需地区/时间/标的物限定" in desc  # ag-010 型问题的全库口径声明


def test_graph_description_aligns_with_db() -> None:
    desc = SearchGraphTool.description
    assert "supplier_topn" in desc  # 同型承接
    assert "中标总额" in desc
    assert "优先 search_database" in desc  # 排名统计 db 优先的分工不漂移


# ── 实现侧：描述承诺底层兑现 ──


def test_db_supplier_topn_sql_orders_by_total_amount() -> None:
    sql, _ = build_bp_query({"query_type": "supplier_topn"})
    assert "GROUP BY winner" in sql
    assert "ORDER BY total_amount DESC" in sql  # SUM(award_amount) 降序


def test_db_supplier_topn_unqualified_is_whole_library() -> None:
    sql, params = build_bp_query({"query_type": "supplier_topn"})
    assert "WHERE" not in sql  # 无 filters=全库口径（描述承诺）
    assert params[-1] == 10  # 缺省 top_n=10（描述承诺「默认 Top 10」）


def test_graph_supplier_topn_cypher_orders_by_total_amount() -> None:
    client = NeoClient("bolt://stub", "u", "p")  # driver 惰性连接，不触网
    seen: list[tuple[str, dict]] = []
    client.execute = lambda cypher, **params: seen.append((cypher, params)) or []
    client.query("supplier_topn", top_n=5)
    cypher, params = seen[0]
    assert "sum(p.amount) AS total_amount" in cypher
    assert "ORDER BY total_amount DESC" in cypher
    assert params["n"] == 5
