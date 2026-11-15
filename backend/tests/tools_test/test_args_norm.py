"""工具枚举参数容错归一（tools/args_norm 单入口）+ args schema 自文档化。

2026-11-15 空回事故加固：模型此前看不到枚举值（Field 描述只写「查询类型
枚举」），探针实测 3/3 轮拼错 query_type（supplier_top 截断 / 供应商TopN
中文别名）——本文件锁定归一阶梯与「schema 必须自带枚举值」。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bidqa.tools.args_norm import normalize_choice
from bidqa.tools.db_tool import SearchDatabaseArgs
from bidqa.tools.graph_tool import SearchGraphArgs

DB_TYPES = ("subject_agg", "region_agg", "supplier_topn", "purchaser_topn",
            "time_trend", "detail_search")


# ── normalize_choice 分辨率梯度 ──
def test_exact_passes_through() -> None:
    assert normalize_choice("supplier_topn", DB_TYPES) == "supplier_topn"


def test_casefold_match() -> None:
    assert normalize_choice("Supplier_TopN", DB_TYPES) == "supplier_topn"


def test_unique_prefix_match() -> None:
    assert normalize_choice("supplier_top", DB_TYPES) == "supplier_topn"


def test_ambiguous_prefix_rejected() -> None:
    # subject_agg / supplier_topn 双候选 = 歧义，不猜
    with pytest.raises(ValueError):
        normalize_choice("s", DB_TYPES)


def test_alias_match_case_insensitive() -> None:
    aliases = {"供应商TopN": "supplier_topn", "明细检索": "detail_search"}
    assert normalize_choice("供应商TopN", DB_TYPES, aliases) == "supplier_topn"
    assert normalize_choice("供应商topn", DB_TYPES, aliases) == "supplier_topn"
    assert normalize_choice("明细检索", DB_TYPES, aliases) == "detail_search"


def test_miss_raises_with_enum_list() -> None:
    with pytest.raises(ValueError, match="supplier_topn"):
        normalize_choice("drop_table", DB_TYPES)


def test_strip_whitespace() -> None:
    assert normalize_choice(" supplier_topn ", DB_TYPES) == "supplier_topn"


def test_empty_rejected() -> None:
    with pytest.raises(ValueError):
        normalize_choice("", DB_TYPES)


# ── db 工具：截断/中文别名自纠 + 非法值报错 ──
def test_db_args_prefix_self_heal() -> None:
    args = SearchDatabaseArgs.model_validate({"query_type": "supplier_top"})
    assert args.query_type == "supplier_topn"


def test_db_args_chinese_alias_self_heal() -> None:
    args = SearchDatabaseArgs.model_validate({"query_type": "供应商TopN"})
    assert args.query_type == "supplier_topn"


def test_db_args_invalid_rejected() -> None:
    with pytest.raises(ValidationError):
        SearchDatabaseArgs.model_validate({"query_type": "drop_table"})


# ── graph 工具同款（枚举表不同：subject_freq/amount_agg/...）──
def test_graph_args_prefix_self_heal() -> None:
    args = SearchGraphArgs.model_validate({"query_type": "subject_f"})
    assert args.query_type == "subject_freq"


def test_graph_args_chinese_alias_self_heal() -> None:
    args = SearchGraphArgs.model_validate({"query_type": "供应商TopN"})
    assert args.query_type == "supplier_topn"


def test_graph_args_invalid_rejected() -> None:
    with pytest.raises(ValidationError):
        SearchGraphArgs.model_validate({"query_type": "hack_query"})


def test_graph_args_subject_supplier_requires_entity() -> None:
    """subject_supplier 的 entity 必填（args 层友好报错；neo4j 层同守卫）。"""
    with pytest.raises(ValidationError):
        SearchGraphArgs.model_validate({"query_type": "subject_supplier"})
    args = SearchGraphArgs.model_validate(
        {"query_type": "subject_supplier", "entity": "空调"})
    assert args.query_type == "subject_supplier"


def test_graph_args_subject_supplier_alias() -> None:
    args = SearchGraphArgs.model_validate(
        {"query_type": "标的物供应商", "entity": "空调"})
    assert args.query_type == "subject_supplier"


# ── 多余参数一律拒绝（2026-11-15 审计 P2：extra=ignore 会让跨工具参数串味
#    被静默吞掉——「keyword=空调」传给 search_graph 曾会返回全库 66 亿榜）──
def test_db_args_forbid_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        SearchDatabaseArgs.model_validate({"query_type": "subject_agg", "kwyword": "空调"})


def test_graph_args_forbid_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        SearchGraphArgs.model_validate({"query_type": "subject_freq", "kwyword": "空调"})


# ── args schema 自文档化（模型必须能在 schema 里看到枚举值）──
def test_db_schema_documents_query_types() -> None:
    desc = SearchDatabaseArgs.model_json_schema()["properties"]["query_type"]["description"]
    for qt in DB_TYPES:
        assert qt in desc, qt


def test_graph_schema_documents_query_types() -> None:
    desc = SearchGraphArgs.model_json_schema()["properties"]["query_type"]["description"]
    for qt in ("subject_freq", "amount_agg", "supplier_topn", "region_dist",
               "purchaser_dist", "time_trend", "subject_supplier"):
        assert qt in desc, qt
