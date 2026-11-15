"""F5/D2 图谱单元 + 真实 Neo4j 集成（CI 无 Neo4j 自动 skip；集成只清 IT-G* 自建节点）。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from bidqa.ingest.graph_build import (
    MERGE_CONFIDENCE,
    _apply_mapping,
    build_graph,
    extract_entities,
    load_mapping_file,
    normalize_entities,
    write_mapping_json,
    write_review_csv,
)
from bidqa.tools.graph_tool import SearchGraphTool


class FakeChat:
    def __init__(self, reply: str):
        self.reply = reply

    def chat(self, messages, temperature=0.3):
        return self.reply


class RecordingChat:
    """记录每次请求，供「分字段分批」断言。"""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[list[dict]] = []

    def chat(self, messages, temperature=0.3):
        self.calls.append(messages)
        return self.handler(len(self.calls), messages)


def _names_from_message(messages) -> list[str]:
    content = messages[-1]["content"]
    return json.loads(content.split("\n", 1)[1])


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
    mapping, review, stats = normalize_entities({"winner": ["甲公司", "乙公司"]}, None)
    assert mapping == {"winner": {}} and review == []  # 诚实降级：不归一
    assert stats["calls"] == 0


def test_normalize_high_confidence_merges_low_goes_review() -> None:
    reply = json.dumps({
        "groups": [["安徽国控", "安徽国控集团", "安徽国资"]],
        "confidence": {"安徽国控集团": 0.93, "安徽国资": 0.60},
        }, ensure_ascii=False)
    chat = FakeChat(reply + "\n安徽国资,安徽国控")  # 尾部噪音行应被 JSON 提取忽略
    entities = {"winner": ["安徽国控", "安徽国控集团", "安徽国资"]}
    mapping, review, stats = normalize_entities(entities, chat)
    assert mapping == {"winner": {"安徽国控集团": "安徽国控"}}  # ≥0.85 自动合并
    assert review == [{"field": "winner", "alias": "安徽国资",
                       "canonical": "安徽国控", "confidence": 0.60,
                       "reason": "low_confidence"}]  # <0.85 不合并
    assert stats["calls"] == 1 and stats["failed_batches"] == []
    assert MERGE_CONFIDENCE == 0.85


def test_normalize_garbage_reply_degrades_one_batch_only() -> None:
    mapping, review, stats = normalize_entities({"winner": ["A", "B"]}, FakeChat("不是JSON"))
    assert mapping == {"winner": {}} and review == []
    assert stats["calls"] == 1
    assert stats["failed_batches"][0]["field"] == "winner"  # 失败可见，不再静默


def test_normalize_batches_per_field_and_respects_size() -> None:
    names = [f"公司{i:03d}" for i in range(450)]
    entities = {"winner": names, "purchaser": ["采购人甲", "采购人乙"]}
    chat = RecordingChat(lambda i, m: json.dumps(
        {"groups": [], "confidence": {}}, ensure_ascii=False))
    _, _, stats = normalize_entities(entities, chat, batch_size=200)
    assert stats["fields"]["winner"]["batches"] == 3
    assert stats["fields"]["purchaser"]["batches"] == 1
    assert stats["calls"] == 4
    winner_batches = []
    for messages in chat.calls:
        payload = _names_from_message(messages)
        assert len(payload) <= 200
        assert "字段：" in messages[-1]["content"]
        if payload[0].startswith("公司"):
            winner_batches.append(payload)
        else:
            assert payload == ["采购人甲", "采购人乙"]  # 不与 winner 混批
    assert sum(len(b) for b in winner_batches) == 450


def test_normalize_skips_subject_matter_and_region() -> None:
    entities = {"subject_matter": ["打印机", "激光打印机"],
                "region": ["安徽省合肥市", "合肥市"],
                "winner": ["甲公司", "乙公司"]}
    chat = RecordingChat(lambda i, m: json.dumps(
        {"groups": [], "confidence": {}}, ensure_ascii=False))
    _, _, stats = normalize_entities(entities, chat)
    assert stats["calls"] == 1  # 只有 winner 调模型
    assert stats["fields"]["subject_matter"]["normalized"] is False
    assert stats["fields"]["region"]["normalized"] is False
    assert stats["fields"]["winner"]["normalized"] is True


def test_normalize_oversized_group_goes_review_not_auto_merge() -> None:
    names = [f"别名{i}" for i in range(10)]  # canonical + 9 aliases > 上限 8
    names[0] = "甲公司"
    reply = json.dumps({"groups": [names],
                        "confidence": {n: 0.99 for n in names[1:]}},
                       ensure_ascii=False)
    mapping, review, stats = normalize_entities({"winner": names}, FakeChat(reply))
    assert mapping["winner"] == {}  # 病态大组不自动合并
    assert stats["oversized_groups"] == 1
    assert len(review) == 9
    assert all(r["confidence"] == 0.0 and r["reason"] == "oversized_group"
               for r in review)


def test_conservative_gate_blocks_branch_but_allows_suffix() -> None:
    reply = json.dumps({
        "groups": [["长丰供水", "长丰供水有限公司"],
                   ["合肥市和平小学", "合肥市和平小学第二小学"]],
        "confidence": {"长丰供水有限公司": 0.99, "合肥市和平小学第二小学": 0.99},
    }, ensure_ascii=False)
    mapping, review, _ = normalize_entities(
        {"winner": ["长丰供水", "长丰供水有限公司",
                    "合肥市和平小学", "合肥市和平小学第二小学"]},
        FakeChat(reply))
    assert mapping["winner"] == {"长丰供水有限公司": "长丰供水"}  # 公司后缀差异允许
    assert review == [{"field": "winner", "alias": "合肥市和平小学第二小学",
                       "canonical": "合肥市和平小学", "confidence": 0.99,
                       "reason": "not_simple_variant"}]  # 校区/分校不自动并


def test_normalize_batch_failure_is_isolated() -> None:
    names = [f"公司{i:03d}" for i in range(450)]
    names[1] = "公司000有限公司"  # 同批内可过确定性闸门的后缀变体

    def handler(call_no, messages):
        if call_no == 2:
            raise RuntimeError("boom")
        return json.dumps({"groups": [["公司000", "公司000有限公司"]],
                           "confidence": {"公司000有限公司": 0.95}},
                          ensure_ascii=False)

    mapping, _, stats = normalize_entities({"winner": names}, RecordingChat(handler),
                                           batch_size=200)
    assert stats["calls"] == 3
    assert stats["failed_batches"] == [
        {"field": "winner", "batch": 2, "names": 200, "error": "boom"}]
    assert mapping["winner"] == {"公司000有限公司": "公司000"}  # 第 1 批结果保住


def test_normalize_rejects_hallucinated_names_and_bad_confidence() -> None:
    reply = json.dumps({
        "groups": [["甲公司", "乙公司", "丙公司"], ["丁公司", "甲公司"]],
        "confidence": {"乙公司": "high", "甲公司": None},
    }, ensure_ascii=False)
    mapping, review, _ = normalize_entities(
        {"winner": ["甲公司", "乙公司"]}, FakeChat(reply))
    assert mapping == {"winner": {}}  # 丙公司幻觉丢弃；丁公司组整组丢弃
    assert review == [{"field": "winner", "alias": "乙公司",
                       "canonical": "甲公司", "confidence": 0.0,
                       "reason": "low_confidence"}]


def test_normalize_collapses_transitive_mapping() -> None:
    names = ["长丰供水", "长丰供水集团", "长丰供水集团有限公司"]
    reply = json.dumps({"groups": [[names[1], names[2]], [names[0], names[1]]],
                        "confidence": {names[1]: 0.9, names[2]: 0.9}},
                       ensure_ascii=False)
    mapping, _, _ = normalize_entities({"winner": names}, FakeChat(reply))
    assert mapping["winner"] == {names[1]: names[0], names[2]: names[0]}


def test_normalize_mapping_is_field_scoped() -> None:
    mapping = {"winner": {"同名": "甲公司"}, "purchaser": {"同名": "乙公司"}}
    rows = [{"winner": "同名", "purchaser": "同名", "subject_matter": "同名"}]
    out = _apply_mapping(rows, mapping)
    assert out[0]["winner"] == "甲公司"
    assert out[0]["purchaser"] == "乙公司"
    assert out[0]["subject_matter"] == "同名"  # 未映射字段不受影响
    assert rows[0]["winner"] == "同名"  # 不改原行


def test_mapping_json_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bidqa.ingest.graph_build.REVIEW_DIR", tmp_path)
    path = write_mapping_json({"winner": {"甲公司": "乙公司"}}, {"calls": 1}, "bt")
    assert path.name == "graph_alias_mapping_bt.json"
    loaded = load_mapping_file(path)
    assert loaded["winner"] == {"甲公司": "乙公司"}
    assert loaded["region"] == {}


def test_review_csv_written(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bidqa.ingest.graph_build.REVIEW_DIR", tmp_path)
    path = write_review_csv([{"field": "winner", "alias": "a",
                              "canonical": "b", "confidence": 0.5,
                              "reason": "low_confidence"}], "bt")
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


def test_subject_supplier_query(neo: NeoClient) -> None:
    """subject_supplier（标的物→供应商分布）：按标的物过滤 + entity 必填。

    图谱独有语义：沿 (Supplier)-[:WON]->(Project)-[:FOR]->(SubjectMatter) 遍历，
    与 supplier_topn（entity 过滤供应商名）互补；FOR/WON 每项目至多一条边
    （单值列 MERGE）→ count/sum 无重复计数。
    """
    for row in ROWS:
        neo.upsert_project(row)
    rows = neo.query("subject_supplier", entity="IT-G空调")
    assert [(r["label"], r["projects"], r["total_amount"]) for r in rows] == \
        [("IT-G供应商甲", 1, 660_000)]
    # 跨标的物聚合：IT-G 前缀命中空调+电梯两个标的物，归一到同一供应商
    rows = neo.query("subject_supplier", entity="IT-G")
    assert [(r["label"], r["projects"], r["total_amount"]) for r in rows] == \
        [("IT-G供应商甲", 2, 1_860_000)]
    with pytest.raises(ValueError):
        neo.query("subject_supplier")  # entity 必填（否则与 supplier_topn 重复）


def _stub_pg(rows: list[dict]):
    class StubPg:
        def connection(self):
            class Cur:
                def execute(self, q):
                    class R:
                        def fetchall(self):
                            return [dict(r) for r in rows]
                    return R()
            return Cur()
    return StubPg()


def test_build_graph_full_flow(neo: NeoClient) -> None:
    result = build_graph(_stub_pg(ROWS), neo, chat=None, batch="it-graph")
    assert result["rows"] == 3
    assert result["review_csv"] is None  # 无 LLM → 无归一无复核
    assert _count_one(neo, f"MATCH (p:Project) {_SCOPED} RETURN count(p) AS n") == 3


NORML_ROWS = [
    {"project_no": "IT-G10", "project_name": "归一测试", "subject_matter": "电梯",
     "winner": "长丰供水", "purchaser": "采购人", "agency": "代理机构",
     "region": "安徽省合肥市", "award_amount": 1, "publish_date": date(2025, 1, 1)},
    {"project_no": "IT-G11", "project_name": "归一测试2", "subject_matter": "电梯",
     "winner": "长丰供水有限公司", "purchaser": "采购人", "agency": "代理机构",
     "region": "合肥市", "award_amount": 2, "publish_date": date(2025, 1, 2)},
]


def test_build_graph_dry_run_normalize_writes_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bidqa.ingest.graph_build.REVIEW_DIR", tmp_path)
    reply = json.dumps({"groups": [["长丰供水", "长丰供水有限公司"]],
                        "confidence": {"长丰供水有限公司": 0.95}}, ensure_ascii=False)
    result = build_graph(_stub_pg(NORML_ROWS), object(), chat=FakeChat(reply),
                         batch="bt", dry_run=True, normalize=True)
    assert result["normalize_calls"] >= 1
    assert result["mapping_items"] == 1
    assert Path(result["mapping_json"]).exists()
    assert result["review_csv"] is None  # 无 <0.85 候选
    payload = json.loads(Path(result["mapping_json"]).read_text(encoding="utf-8"))
    assert payload["mapping"]["winner"] == {"长丰供水有限公司": "长丰供水"}


def test_build_graph_reuses_mapping_file_without_llm(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bidqa.ingest.graph_build.REVIEW_DIR", tmp_path)
    path = write_mapping_json({"winner": {"甲有限公司": "甲公司"}}, {"calls": 1}, "bt")

    class ExplodingChat:
        def chat(self, *args, **kwargs):
            raise AssertionError("复用 mapping 文件时不应调用模型")

    result = build_graph(_stub_pg(NORML_ROWS), object(), chat=ExplodingChat(),
                         batch="bt", dry_run=True, mapping_file=path)
    assert result["normalize_calls"] == 0
    assert result["mapping_source"] == str(path)
    assert result["mapping"]["winner"] == {"甲有限公司": "甲公司"}


def test_build_graph_default_does_not_call_llm() -> None:
    class ExplodingChat:
        def chat(self, *args, **kwargs):
            raise AssertionError("默认建图不应调用模型")

    result = build_graph(_stub_pg(NORML_ROWS), object(), chat=ExplodingChat(),
                         batch="bt", dry_run=True)
    assert result["normalize_calls"] == 0
    assert result["mapping_items"] == 0
