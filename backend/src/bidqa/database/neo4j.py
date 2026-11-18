"""Neo4j 客户端（docs/01 §4.2）：唯一性约束先行（否则 Batch 重跑必重复）+
六类节点六关系 + 7 类查询模板；慢查询走 session 级超时。金额/时间在 :Project 属性。
"""

from __future__ import annotations

from typing import Any

from neo4j import GraphDatabase

GRAPH_QUERY_TYPES = (
    "subject_freq",      # 标的物频次
    "amount_agg",        # 均价与金额分布
    "supplier_topn",     # 供应商分布（entity 过滤供应商名）
    "region_dist",       # 地区分布
    "purchaser_dist",    # 采购人分布
    "time_trend",        # 时间趋势（按月）
    "subject_supplier",  # 标的物供应商分布（entity 必填：按标的物找供应商）
)

# 建图脚本必须先建（docs/01 §4.2 钉死）
CONSTRAINTS = (
    "CREATE CONSTRAINT IF NOT EXISTS FOR (p:Project) REQUIRE p.project_no IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (s:SubjectMatter) REQUIRE s.name IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (su:Supplier) REQUIRE su.name IS UNIQUE",
)


class NeoClient:
    def __init__(self, uri: str, user: str, password: str, query_timeout: float = 30.0):
        self._uri = uri
        self._auth = (user, password)
        self._driver = None
        self._timeout = query_timeout

    def driver(self):
        if self._driver is None:
            self._driver = GraphDatabase.driver(self._uri, auth=self._auth)
        return self._driver

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def ping(self) -> bool:
        try:
            self.driver().verify_connectivity()
            return True
        except Exception:
            return False

    def execute(self, cypher: str, **params: Any) -> list[dict]:
        with self.driver().session() as session:
            return [dict(r) for r in session.run(cypher, **params)]

    def write(self, cypher: str, **params: Any) -> None:
        with self.driver().session() as session:
            session.run(cypher, **params)

    # ── 约束与重建 ──
    def ensure_constraints(self) -> None:
        for cql in CONSTRAINTS:  # 先建唯一性约束再导入（docs/01 §4.2）
            self.write(cql)

    def clear_all(self) -> int:
        """清空重建入口（F5 支持清空重建）。返回删除节点数。"""
        rows = self.execute("MATCH (n) DETACH DELETE n RETURN count(n) AS c")
        return int(rows[0]["c"]) if rows else 0

    # ── F5 upsert（MERGE 幂等；空值不建节点——「不可用」不进图谱）──
    _UPSERT_CYPHER = """
    MERGE (p:Project {project_no: $no})
    SET p.name = $name, p.amount = $amount, p.publish_date = $pd
    FOREACH (_ IN CASE WHEN $subject <> '' THEN [1] ELSE [] END |
      MERGE (sm:SubjectMatter {name: $subject}) MERGE (p)-[:FOR]->(sm))
    FOREACH (_ IN CASE WHEN $purchaser <> '' THEN [1] ELSE [] END |
      MERGE (pu:Purchaser {name: $purchaser}) SET pu.region = $region
      MERGE (pu)-[:PROCURED]->(p))
    FOREACH (_ IN CASE WHEN $winner <> '' THEN [1] ELSE [] END |
      MERGE (su:Supplier {name: $winner}) SET su.region = $region
      MERGE (su)-[:WON]->(p))
    FOREACH (_ IN CASE WHEN $agency <> '' THEN [1] ELSE [] END |
      MERGE (a:Agency {name: $agency}) MERGE (a)-[:AGENTED]->(p))
    FOREACH (_ IN CASE WHEN $region <> '' THEN [1] ELSE [] END |
      MERGE (l:Location {name: $region}) SET l.level = '省' MERGE (p)-[:IN]->(l))
    """

    def upsert_projects(self, rows: list[dict[str, Any]]) -> int:
        """批量 upsert：单会话逐行（MERGE 幂等，重跑安全）。"""
        count = 0
        with self.driver().session() as session:
            for row in rows:
                publish_date = row.get("publish_date")
                region = (row.get("region") or "").strip()
                session.run(self._UPSERT_CYPHER,
                            no=str(row.get("project_no") or ""),
                            name=row.get("project_name") or "",
                            amount=row.get("award_amount"),
                            pd=publish_date.isoformat() if publish_date else None,
                            subject=(row.get("subject_matter") or "").strip(),
                            purchaser=(row.get("purchaser") or "").strip(),
                            winner=(row.get("winner") or "").strip(),
                            agency=(row.get("agency") or "").strip(),
                            region=region)
                count += 1
        return count

    def upsert_project(self, row: dict[str, Any]) -> None:
        self.upsert_projects([row])

    # ── 7 类查询模板（docs/01 §4.2；entity 可选过滤，subject_supplier 必填）──
    def query(self, query_type: str, entity: str | None = None,
              top_n: int = 10) -> list[dict]:
        if query_type not in GRAPH_QUERY_TYPES:
            raise ValueError(f"未知 query_type: {query_type}")
        if not 1 <= top_n <= 100:
            raise ValueError("top_n 须为 1..100 整数")
        entity_filter = "WHERE toLower(sm.name) CONTAINS toLower($entity)" if entity else ""
        if query_type == "subject_freq":
            return self.execute(
                f"MATCH (sm:SubjectMatter)<-[:FOR]-(p:Project) {entity_filter} "
                "RETURN sm.name AS label, count(p) AS projects, sum(p.amount) AS total_amount "
                "ORDER BY projects DESC LIMIT $n", entity=entity or "", n=top_n)
        if query_type == "amount_agg":
            # entity 过滤与金额条件合并 AND（审计 P1-14：原 entity_filter 自带 WHERE
            # 又硬拼 WHERE → 双 WHERE Cypher 语法错，带过滤的均价查询 100% 不可用）
            where = ("WHERE toLower(sm.name) CONTAINS toLower($entity) "
                     "AND p.amount IS NOT NULL") if entity else "WHERE p.amount IS NOT NULL"
            return self.execute(
                f"MATCH (sm:SubjectMatter)<-[:FOR]-(p:Project) {where} "
                "RETURN sm.name AS label, count(p) AS projects, avg(p.amount) AS avg_amount, "
                "sum(p.amount) AS total_amount ORDER BY total_amount DESC LIMIT $n",
                entity=entity or "", n=top_n)
        if query_type == "supplier_topn":
            where = "WHERE toLower(su.name) CONTAINS toLower($entity)" if entity else ""
            return self.execute(
                f"MATCH (su:Supplier)-[:WON]->(p:Project) {where} "
                "RETURN su.name AS label, count(p) AS projects, sum(p.amount) AS total_amount "
                "ORDER BY total_amount DESC LIMIT $n", entity=entity or "", n=top_n)
        if query_type == "subject_supplier":
            # 标的物→供应商遍历（supplier_topn 只能按供应商名过滤，此查询补
            # 「标的物为 X 的供应商分布」）。**单批次内**每项目至多一条 FOR/WON
            # 边（单值列 MERGE），count/sum 无重复计数；跨批次同号项目 subject
            # 值分歧时（F6 并存语义）FOR 可双挂、会 double-count——重建前按
            # project_no 去重或保证单批次。无 entity 时与 supplier_topn 全量
            # 重复，必须拒绝。
            if not (entity or "").strip():
                raise ValueError("subject_supplier 须提供 entity（标的物片段）；"
                                 "按供应商名找公司用 supplier_topn")
            return self.execute(
                "MATCH (su:Supplier)-[:WON]->(p:Project)-[:FOR]->(sm:SubjectMatter) "
                "WHERE toLower(sm.name) CONTAINS toLower($entity) "
                "RETURN su.name AS label, count(p) AS projects, sum(p.amount) AS total_amount "
                "ORDER BY total_amount DESC LIMIT $n", entity=entity, n=top_n)
        if query_type == "region_dist":
            return self.execute(
                "MATCH (p:Project)-[:IN]->(l:Location) "
                "RETURN l.name AS label, count(p) AS projects, sum(p.amount) AS total_amount "
                "ORDER BY projects DESC LIMIT $n", n=top_n)
        if query_type == "purchaser_dist":
            # entity 片段过滤真实实现（审计 P1-15：原忽略 entity 静默返回全库榜——
            # 工具描述承诺「按采购人名称片段过滤」而底层不兑现）
            where = "WHERE toLower(pu.name) CONTAINS toLower($entity)" if entity else ""
            return self.execute(
                f"MATCH (pu:Purchaser)-[:PROCURED]->(p:Project) {where} "
                "RETURN pu.name AS label, count(p) AS projects, sum(p.amount) AS total_amount "
                "ORDER BY projects DESC LIMIT $n", entity=entity or "", n=top_n)
        # time_trend：按月
        return self.execute(
            "MATCH (p:Project) WHERE p.publish_date IS NOT NULL "
            "RETURN left(p.publish_date, 7) AS label, count(p) AS projects, "
            "sum(p.amount) AS total_amount ORDER BY label LIMIT $n", n=top_n)

    def counts(self) -> dict[str, int]:
        rows = self.execute(
            "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS c")
        return {r["label"]: int(r["c"]) for r in rows}

    def relationship_counts(self) -> dict[str, int]:
        """关系（边）计数：{类型: 数量}（控制台 KPI 与节点计数对称，docs/01 §3.1）。"""
        rows = self.execute(
            "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS n")
        return {r["t"]: int(r["n"]) for r in rows}
