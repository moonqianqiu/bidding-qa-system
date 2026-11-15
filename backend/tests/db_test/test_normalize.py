"""F6 归一唯一入口 + D3 查询构建白名单/转义（纯函数单测，无需 PG）。"""

from __future__ import annotations

from datetime import date

import pytest

from bidqa.database.bp_query import build_bp_query
from bidqa.database.bp_schema import escape_like
from bidqa.ingest.normalize import normalize_amount, normalize_date, normalize_region


# ── 金额归一（唯一入口；docs/02 F6）──
@pytest.mark.parametrize("raw,expect", [
    ("66万", 660_000), ("66.5万", 665_000), ("66 万元", 660_000),
    ("1.2亿", 120_000_000), ("660,000元", 660_000), ("660,000", 660_000),
    (660000, 660_000), (660000.4, 660_000), ("约", None), ("无", None),
    ("", None), (None, None), (True, None), (-5, None), ("-660000", None),
    ("66.5万元整", None),  # 不认噪音后缀，宁可 None 也不硬造
])
def test_normalize_amount(raw: object, expect: int | None) -> None:
    assert normalize_amount(raw) == expect


def test_amount_never_fabricates_zero() -> None:
    """取不到记 None 绝不写 0（跨层硬约束）。"""
    assert normalize_amount("中标金额：见公告") is None


# ── 地区/日期归一 ──
@pytest.mark.parametrize("raw,expect", [
    ("安徽省合肥市", "安徽"), ("新疆维吾尔自治区", "新疆"), ("内蒙古", "内蒙古"),
    ("境外某地", ""), ("", ""), (None, ""),
])
def test_normalize_region(raw: object, expect: str) -> None:
    assert normalize_region(raw) == expect


@pytest.mark.parametrize("raw,expect", [
    ("2025-03-01", date(2025, 3, 1)), ("2025/3/1", date(2025, 3, 1)),
    ("2025年3月1日", date(2025, 3, 1)), ("20250301", date(2025, 3, 1)),
    ("", None), ("见公告", None), (None, None),
])
def test_normalize_date(raw: object, expect: date | None) -> None:
    assert normalize_date(raw) == expect


# ── LIKE 转义（docs/02 D3）──
@pytest.mark.parametrize("raw,expect", [
    ("空调%", "空调\\%"), ("win_er", "win\\_er"), ("a\\b", "a\\\\b"),
    ("正常", "正常"),
])
def test_escape_like(raw: str, expect: str) -> None:
    assert escape_like(raw) == expect


# ── 查询构建：白名单与参数化 ──
def test_query_subject_agg() -> None:
    sql, params = build_bp_query({"query_type": "subject_agg", "top_n": 5})
    assert "GROUP BY subject_matter" in sql and params == [5]
    assert "SUM(award_amount)" in sql


def test_query_supplier_topn_orders_by_amount() -> None:
    sql, _ = build_bp_query({"query_type": "supplier_topn"})
    assert "GROUP BY winner" in sql and "total_amount DESC NULLS LAST" in sql


def test_query_time_trend_monthly() -> None:
    sql, _ = build_bp_query({"query_type": "time_trend"})
    assert "date_trunc('month', publish_date)" in sql


def test_query_detail_search_escapes_keyword() -> None:
    sql, params = build_bp_query({"query_type": "detail_search", "keyword": "空%调_",
                                  "top_n": 3})
    assert "ESCAPE '\\'" in sql
    assert params[0] == "%空\\%调\\_%"  # 转义后包 %…%
    assert params[-1] == 3


# ── 反向匹配变体（参考吸收 #4b：关键词是被匹配串，%/_ 无需转义）──
def test_query_reverse_variant() -> None:
    sql, params = build_bp_query(
        {"query_type": "detail_search", "keyword": "空调采购项目招标公告"}, reverse=True)
    assert "char_length(project_name) >= 3" in sql  # 超短字段值降噪
    # psycopg 参数化下 SQL 文本字面 % 写作 %%（绑定前还原为 %）
    assert "ILIKE '%%' || project_name || '%%'" in sql
    assert params[0] == "空调采购项目招标公告"  # 关键词原样作被匹配串，无 %…% 包裹


def test_query_reverse_keeps_filters_and_ignores_on_aggregate() -> None:
    sql, _ = build_bp_query({"query_type": "detail_search", "keyword": "k",
                             "filters": {"region": "安徽"}}, reverse=True)
    assert "region LIKE" in sql and "char_length(project_name)" in sql  # 过滤条件原样保留
    agg_sql, _ = build_bp_query({"query_type": "region_agg"}, reverse=True)
    assert "ILIKE" not in agg_sql  # 非 detail_search 不受 reverse 影响


# ── keyword 对全部查询类型生效（2026-11-15 修复：聚合类曾静默丢参，
#    「空调的主要供应商」返回过无过滤全库 TopN）──
def test_query_keyword_filters_supplier_topn() -> None:
    sql, params = build_bp_query({"query_type": "supplier_topn", "keyword": "空调",
                                  "top_n": 5})
    assert all(f"{col} LIKE" in sql for col in
               ("project_name", "winner", "subject_matter", "project_no"))
    assert "GROUP BY winner" in sql
    assert params == ["%空调%"] * 4 + [5]


def test_query_keyword_filters_all_agg_types() -> None:
    for qt, group in (("subject_agg", "GROUP BY subject_matter"),
                      ("region_agg", "GROUP BY region"),
                      ("purchaser_topn", "GROUP BY purchaser"),
                      ("time_trend", "date_trunc('month', publish_date)")):
        sql, params = build_bp_query({"query_type": qt, "keyword": "空调", "top_n": 3})
        assert "project_name LIKE" in sql, qt
        assert group in sql, qt
        assert params[:4] == ["%空调%"] * 4 and params[-1] == 3, qt


def test_query_keyword_escapes_on_aggregate() -> None:
    _, params = build_bp_query({"query_type": "supplier_topn", "keyword": "空%调_"})
    assert params[0] == "%空\\%调\\_%"
    assert params[3] == "%空\\%调\\_%"  # 四列同参


def test_query_keyword_strips_whitespace() -> None:
    """LLM 偶发带空白传参（「 空调 」）→ strip 后再 LIKE，避免无谓空结果。"""
    _, params = build_bp_query({"query_type": "supplier_topn", "keyword": " 空调 "})
    assert params[0] == "%空调%"


def test_query_keyword_survives_reverse_on_aggregate() -> None:
    """keyword 在任何参数组合下都不被静默丢弃：聚合 + reverse 变体仍按正向过滤。"""
    sql, params = build_bp_query({"query_type": "region_agg", "keyword": "空调"},
                                 reverse=True)
    assert "ILIKE" not in sql and "project_name LIKE" in sql
    assert params[:4] == ["%空调%"] * 4


def test_query_filters_whitelist_only() -> None:
    with pytest.raises(ValueError, match="白名单"):
        build_bp_query({"query_type": "detail_search", "filters": {"password": "x"}})
    with pytest.raises(ValueError, match="白名单"):
        build_bp_query({"query_type": "detail_search", "filters": {"密码": "x"}})
    sql, params = build_bp_query({"query_type": "detail_search",
                                  "filters": {"winner": "安徽公司"}})
    assert "winner = %s" in sql and params[0] == "安徽公司"


# ── 中文别名 + region 包含匹配（docs/01 §4.3 过滤语义）──
def test_filters_accept_chinese_alias() -> None:
    sql, params = build_bp_query({"query_type": "region_agg", "filters": {"地区": "安徽省"}})
    assert "region LIKE %s" in sql and params[0] == "%安徽省%"
    sql, params = build_bp_query({"query_type": "subject_agg", "filters": {"标的物": "空调"}})
    assert "subject_matter = %s" in sql and params[0] == "空调"
    sql, params = build_bp_query({"query_type": "supplier_topn", "filters": {"中标人": "甲公司"}})
    assert "winner = %s" in sql and params[0] == "甲公司"


def test_region_filter_is_contains_match() -> None:
    """真实库 region 粒度混有省/市（安徽省合肥市 / 安徽），按省过滤必须能命中。"""
    sql, params = build_bp_query({"query_type": "detail_search", "filters": {"region": "合肥"}})
    assert "region LIKE %s ESCAPE '\\'" in sql
    assert params[0] == "%合肥%"


def test_region_filter_escapes_like_wildcards() -> None:
    _, params = build_bp_query({"query_type": "detail_search", "filters": {"省份": "50%_"}})
    assert params[0] == "%50\\%\\_%"


def test_query_date_range_whitelist() -> None:
    sql, params = build_bp_query({
        "query_type": "detail_search",
        "date_range": {"field": "publish_date", "start": "2025-01-01", "end": "2025-12-31"}})
    assert "publish_date >= %s" in sql and params[0] == "2025-01-01"
    with pytest.raises(ValueError, match="白名单"):
        build_bp_query({"query_type": "detail_search",
                        "date_range": {"field": "award_amount"}})


def test_query_type_must_be_in_enum() -> None:
    with pytest.raises(ValueError, match="query_type"):
        build_bp_query({"query_type": "drop_table"})
    with pytest.raises(ValueError):
        build_bp_query({"query_type": "detail_search", "top_n": 0})
