"""D3 业务库白名单（docs/01 §4.3 字段表 = 查询/过滤白名单单一来源）。

从 postgres.py 拆出（重构 C2）：常量与转义纯件，供 bp_query 构建与工具层引用；
D3 直接引用本表，不另行维护。
"""

from __future__ import annotations

BP_COLUMNS: tuple[str, ...] = (
    "project_no", "project_name", "project_category", "project_stage",
    "purchaser", "purchaser_contact", "agency", "agency_contact",
    "winner", "winner_contact", "award_amount", "content", "location", "region",
    "subject_matter", "source_url", "publish_date", "change_date", "plan_date",
    "award_date", "batch_id",
)

DATE_COLUMNS = frozenset({"publish_date", "change_date", "plan_date", "award_date"})

# filters 中文别名 → 白名单列（docs/01 §4.3 过滤语义。只做键名映射，不新增字段）
FILTER_ALIASES: dict[str, str] = {
    "地区": "region", "区域": "region", "省份": "region", "省": "region",
    "城市": "region", "市区": "region", "省市": "region",
    "标的物": "subject_matter", "品目": "subject_matter", "采购内容": "subject_matter",
    "中标人": "winner", "中标单位": "winner", "供应商": "winner", "中标供应商": "winner",
    "采购人": "purchaser", "招标人": "purchaser", "采购单位": "purchaser",
    "代理机构": "agency", "项目名称": "project_name", "项目编号": "project_no",
    "项目分类": "project_category", "项目阶段": "project_stage",
    "中标金额": "award_amount", "金额": "award_amount", "项目地点": "location",
}

# 包含匹配列：真实数据的粒度混有省/市（「安徽省合肥市」与「安徽」），等值匹配会漏
CONTAINS_COLUMNS = frozenset({"region"})

QUERY_TYPES = (
    "subject_agg",      # 标的物聚合（项目数/总额）
    "region_agg",       # 地区分布
    "supplier_topn",    # 供应商 TopN
    "purchaser_topn",   # 采购人 TopN
    "time_trend",       # 时间趋势（按月）
    "detail_search",    # 明细检索（关键词+过滤）
)


def escape_like(text: str) -> str:
    """LIKE 通配符转义（配 ESCAPE '\\'，docs/02 D3）。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
