"""F5/D2 图谱单元 + 真实 Neo4j 集成（CI 无 Neo4j 自动 skip；集成只清 IT-G* 自建节点）。"""

from __future__ import annotations

from datetime import date

import pytest

from bidqa.ingest.graph_build import (
    MERGE_CONFIDENCE,
    build_graph,
    extract_entities,
    normalize_entities,
    write_review_csv,
)
from bidqa.tools.graph_tool import SearchGraphTool


class FakeChat:
    def __init__(self, reply: str):
        self.reply = reply

    def chat(self, messages, temperature=0.3):
        return self.reply


# ── 单元：抽取与归一 ──
def test_extract_entities_dedup() -> None:
    rows = [
        {"subject_matter": "空调", "winner": "甲公司", "purchaser": "某局",
         "agency": "A代理", "region": "安徽"},
        {"subject_matter": "空调", "winner": "乙公司", "purchaser": "某局",
         "agency": "", "region": "安徽"},
    ]
    entities = extract_entities(rows)
    assert entities["subject_matter"] == ["空调"]
    assert entities["winner"] == ["甲公司", "乙公司"]
    assert entities["agency"] == ["A代理"]  # 空值不进实体


def test_normalize_no_chat_is_identity() -> None:
    mapping, review = normalize_entities({"winner": ["甲公司", "乙公司"]}, None)
    assert mapping == {} and review == []  # 诚实降级：不归一


def test_normalize_high_confidence_merges_low_goes_review() -> None:
    import json
    reply = json.dumps({
        "groups": [["安徽国控", "安徽国控集团", "安徽国资"]],
        "confidence": {"安徽国控集团": 0.93, "安徽国资": 0.60},
        }, ensure_ascii=False)
    chat = FakeChat(reply + "\n安徽国资,安徽国控")  # 尾部噪音行应被 JSON 提取忽略
    entities = {"winner": ["安徽国控", "安徽国控集团", "安徽国资"]}
    mapping, review = normalize_entities(entities, chat)
    assert mapping == {"安徽国控集团": "安徽国控"}  # ≥0.85 自动合并
    assert review == [{"alias": "安徽国资", "canonical": "安徽国控",
                       "confidence": 0.60}]  # <0.85 不自动合并
    assert MERGE_CONFIDENCE == 0.85


def test_normalize_garbage_reply_degrades(tmp_path) -> None:
    mapping, review = normalize_entities({"winner": ["A", "B"]}, FakeChat("不是JSON"))
    assert mapping == {} and review == []


def test_review_csv_written(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bidqa.ingest.graph_build.REVIEW_DIR", tmp_path)
    path = write_review_csv([{"alias": "a", "canonical": "b", "confidence": 0.5}], "bt")
    assert path is not None and path.exists()
    assert "graph_merge_review_bt.csv" in str(path)
    assert write_review_csv([], "bt") is None


# ── 集成：真实 Neo4j（conftest 提供 neo_client；只清自建 IT-G* 节点）──
# 红线（AGENTS.md「测试不得污染真实仓库」）：测试数据一律带 IT-G 前缀
# （project_no 与实体名同前缀），前置/收尾只删自建节点——禁止 clear_all 清真实图谱。
_NO_PREFIX = "IT-G"


def _wipe_scoped(neo: NeoClient) -> None:
    neo.execute("MATCH (p:Project) WHERE p.project_no STARTS WITH $no DETACH DELETE p",
                no=_NO_PREFIX)
    neo.execute(
        "MATCH (n) WHERE (n:SubjectMatter OR n:Supplier OR n:Purchaser "
        "OR n:Agency OR n:Location) AND n.name STARTS WITH $name DETACH DELETE n",
        name=_NO_PREFIX)


@pytest.fixture(scope="module")
def neo(neo_client):
    _wipe_scoped(neo_client)  # 前置：清历史残留
    yield neo_client
    _wipe_scoped(neo_client)  # 收尾：不污染真实图谱


def test_constraints_idempotent(neo: NeoClient) -> None:
    neo.ensure_constraints()
    neo.ensure_constraints()  # 幂等


ROWS = [
    {"project_no": "IT-G1", "project_name": "空调采购", "subject_matter": "IT-G空调",
     "winner": "IT-G供应商甲", "purchaser": "IT-G采购人", "agency": "IT-G代理A",
     "region": "IT-G皖", "award_amount": 660_000, "publish_date": date(2025, 1, 15)},
    {"project_no": "IT-G2", "project_name": "电梯采购", "subject_matter": "IT-G电梯",
     "winner": "IT-G供应商甲", "purchaser": "IT-G采购人", "agency": "IT-G代理A",
     "region": "IT-G苏", "award_amount": 1_200_000, "publish_date": date(2025, 2, 20)},
    {"project_no": "IT-G3", "project_name": "无中标人项目", "subject_matter": "",
     "winner": "", "purchaser": "", "agency": "", "region": "",
     "award_amount": None, "publish_date": None},
]

_SCOPED = "WHERE p.project_no STARTS WITH 'IT-G'"


def _count_one(neo: NeoClient, pattern: str) -> int:
    return int(neo.execute(pattern)[0]["n"])


def test_upsert_and_six_queries(neo: NeoClient) -> None:
    for row in ROWS:
        neo.upsert_project(row)
    for row in ROWS:  # MERGE 幂等：重跑不重复建
        neo.upsert_project(row)

    assert _count_one(neo, f"MATCH (p:Project) {_SCOPED} RETURN count(p) AS n") == 3
    assert _count_one(neo, "MATCH (p:Project)-[:FOR]->(sm:SubjectMatter) "
                      f"{_SCOPED} RETURN count(DISTINCT sm) AS n") == 2  # 空标的物不建节点
    assert _count_one(neo, "MATCH (su:Supplier)-[:WON]->(p:Project) "
                      f"{_SCOPED} RETURN count(DISTINCT su) AS n") == 1  # 同 winner MERGE 幂等
    assert _count_one(neo, "MATCH (pu:Purchaser)-[:PROCURED]->(p:Project) "
                      f"{_SCOPED} RETURN count(DISTINCT pu) AS n") == 1
    assert _count_one(neo, "MATCH (a:Agency)-[:AGENTED]->(p:Project) "
                      f"{_SCOPED} RETURN count(DISTINCT a) AS n") == 1

    regions = neo.execute("MATCH (p:Project)-[:IN]->(l:Location) "
                          f"{_SCOPED} RETURN l.name AS label ORDER BY label")
    assert {r["label"] for r in regions} == {"IT-G皖", "IT-G苏"}  # 空地区不建节点
    months = neo.execute("MATCH (p:Project) WHERE p.publish_date IS NOT NULL "
                         f"AND p.project_no STARTS WITH 'IT-G' "
                         "RETURN left(p.publish_date, 7) AS label")
    assert {r["label"] for r in months} == {"2025-01", "2025-02"}


def test_six_query_templates_shape(neo: NeoClient) -> None:
    """查询模板形状冒烟：不依赖图谱现有数据（内容质量由 scoped 断言与 e2e 把关）。"""
    for qt in ("subject_freq", "amount_agg", "supplier_topn",
               "region_dist", "purchaser_dist", "time_trend"):
        rows = neo.query(qt, top_n=5)
        assert isinstance(rows, list)
        for r in rows:
            assert "label" in r and "projects" in r


def test_query_type_validation(neo: NeoClient) -> None:
    with pytest.raises(ValueError):
        neo.query("hack_query")
    with pytest.raises(ValueError):
        neo.query("subject_freq", top_n=0)


def test_d2_tool_end_to_end(neo: NeoClient) -> None:
    tool = SearchGraphTool(neo)
    out = tool.run(tool.args_model.model_validate(
        {"query_type": "supplier_topn", "entity": "IT-G供应商甲"}))
    assert out["empty"] is False
    assert "IT-G供应商甲" in out["text"]
    assert out["charts"][0]["source"] == "graph"
    empty = tool.run(tool.args_model.model_validate(
        {"query_type": "supplier_topn", "entity": "不存在的公司XYZ"}))
    assert empty["empty"] is True


def test_build_graph_full_flow(neo: NeoClient) -> None:
    class StubPg:
        def connection(self):
            class Cur:
                def execute(self, q):
                    class R:
                        def fetchall(self):
                            return [dict(r) for r in ROWS]
                    return R()
            return Cur()

    result = build_graph(StubPg(), neo, chat=None, batch="it-graph")
    assert result["rows"] == 3
    assert result["review_csv"] is None  # 无 LLM → 无归一无复核
    assert _count_one(neo, f"MATCH (p:Project) {_SCOPED} RETURN count(p) AS n") == 3
