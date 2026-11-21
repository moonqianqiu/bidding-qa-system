"""D3 search_database：业务库查询工具（docs/02 D3；白名单引用 01 §4.3 字段表）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bidqa.database.bp_repo import BpRepo
from bidqa.database.bp_schema import QUERY_TYPES
from bidqa.tools.args_norm import normalize_choice
from bidqa.tools.base import BaseTool, empty_result, tool_result

# query_type 中文别名（观测到的模型拼写 + 直觉叫法；前缀截断如 supplier_top
# 由 args_norm 归一阶梯自纠，无需别名）
_QUERY_ALIASES = {
    "供应商": "supplier_topn", "供应商TopN": "supplier_topn", "中标人TopN": "supplier_topn",
    "采购人": "purchaser_topn", "采购人TopN": "purchaser_topn",
    "标的物": "subject_agg", "地区": "region_agg",
    "趋势": "time_trend", "时间趋势": "time_trend", "明细": "detail_search",
}

# order_by 中文别名（detail_search 金额排序；直觉叫法全覆盖）
_ORDER_ALIASES = {
    "金额降序": "amount_desc", "金额从高到低": "amount_desc", "降序": "amount_desc",
    "金额升序": "amount_asc", "金额从低到高": "amount_asc", "升序": "amount_asc",
}

_ORDER_CHOICES = ("amount_desc", "amount_asc")

# 聚合输出标题（文本行/图表/口径来源条目三处共用，单一事实源）
_AGG_LABELS = {
    "subject_agg": "按标的物统计", "region_agg": "按地区统计",
    "supplier_topn": "中标供应商TopN", "purchaser_topn": "采购人TopN",
    "time_trend": "按月趋势",
}


class SearchDatabaseArgs(BaseModel):
    # 多余参数一律拒绝：extra=ignore 会静默吞掉跨工具串味参数（2026-11-15 审计 P2，
    # 与 bc99b20「参数要么生效要么报错」同哲学）
    model_config = ConfigDict(extra="forbid")

    query_type: str = Field(description=f"查询类型枚举：{'/'.join(QUERY_TYPES)}")
    keyword: str | None = Field(
        default=None, max_length=200,
        description="关键词过滤（全部查询类型生效）：对项目名/中标人/标的物/编号四列片段匹配，"
                    "聚合类=过滤后聚合。如供应商TopN 加 keyword=『空调』只统计空调相关项目。")
    filters: dict[str, Any] | None = Field(
        default=None,
        description="白名单列过滤，键用英文列名：region / subject_matter / winner / purchaser / "
                    "agency / project_name / project_no / project_category / project_stage / "
                    "location / award_amount / batch_id（中文键如『地区』『标的物』亦可，自动映射）。"
                    "region 为片段匹配，传『安徽省』或『合肥』都能命中；其余列为等值匹配。")
    top_n: int | None = Field(default=None, ge=1, le=100)
    date_range: dict[str, Any] | None = Field(default=None, description="{field,start,end}")
    order_by: str | None = Field(
        default=None,
        description=f"明细检索结果排序（仅 query_type=detail_search 合法，"
                    f"{'/'.join(_ORDER_CHOICES)}）：amount_desc=中标金额从高到低，"
                    "amount_asc=从低到高；缺省按发布时间倒序。聚合/趋势类不支持（有固定排序口径）。")

    @field_validator("query_type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        return normalize_choice(v, QUERY_TYPES, _QUERY_ALIASES)

    @field_validator("order_by")
    @classmethod
    def _check_order(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return normalize_choice(v, _ORDER_CHOICES, _ORDER_ALIASES, param="order_by")

    @model_validator(mode="after")
    def _order_only_for_detail(self) -> "SearchDatabaseArgs":
        if self.order_by is not None and self.query_type != "detail_search":
            raise ValueError("order_by 仅 detail_search 支持（聚合/趋势有固定排序口径）")
        return self


class SearchDatabaseTool(BaseTool[SearchDatabaseArgs]):
    name = "search_database"
    description = ("查询招投标业务库（项目明细表）的**数值统计与明细**：供应商/采购人 TopN 排名"
                   "（supplier_topn/purchaser_topn，按累计中标总额降序，默认 Top 10）、"
                   "各标的物/各地区项目数与金额、按月趋势、关键词明细检索。"
                   "「按中标总额看排名靠前的供应商是谁」这类排名/TopN 题无需地区/时间/标的物限定，"
                   "用 supplier_topn 直接查全库榜即可作答。"
                   "问『多少个/多少钱/前几名/明细』优先用它。"
                   "keyword（如『空调』）对全部查询类型生效：先过滤再聚合/取明细。"
                   "可用 region / subject_matter / winner / purchaser 等白名单列过滤（地区传省或市均可）。"
                   "明细检索可用 order_by=amount_desc/amount_asc 按中标金额排序（仅 detail_search）。"
                   "金额单位=元。")
    args_model = SearchDatabaseArgs

    def __init__(self, repo: BpRepo):
        self.repo = repo

    def run(self, args: SearchDatabaseArgs) -> dict[str, Any]:
        rows, _sql = self.repo.query_bp(args.model_dump(exclude_none=True))
        if not rows:
            return empty_result()
        if args.query_type != "detail_search" and not any(
                str(row.get("label") or "").strip() for row in rows):
            # 分组列全空（如样例库 subject_matter 全 NULL）：单桶「（未知）」不等于有数据，
            # 整张不出图，也不拿全库总量冒充该维度结果
            return empty_result("该维度字段未录入（不可用），无法按此维度出图；"
                                "建议改用地区、供应商、采购人或时间维度查询。")
        if args.query_type == "detail_search":
            text = _format_details(rows)
            return tool_result(text=text, sources=self._detail_sources(rows))
        text, chart = self._aggregate_output(args.query_type, rows)
        return tool_result(text=text, charts=[chart],
                           sources=[self._aggregate_source(args, rows)])

    # ── 聚合输出：文本 + 图表规格 + 口径来源（docs/01 §3.7；id 由 B7 内容寻址补齐）──
    def _aggregate_output(self, query_type: str, rows: list[dict]) -> tuple[str, dict]:
        lines = []
        items = []
        for row in rows:
            label = row.get("label") or "（未知）"
            amount = row.get("total_amount")
            amount_text = f"{int(amount):,} 元" if amount is not None else "金额不可用"
            lines.append(f"- {label}：{row['projects']} 个项目，{amount_text}")
            items.append({"label": label, "projects": int(row["projects"]),
                          "amount": amount})
        title = f"{_AGG_LABELS[query_type]}（项目数与中标金额）"
        chart = {
            "id": "", "kind": "bar", "title": title, "source": "db",
            "format": "int", "max": None,
            "items": [{"label": it["label"], "value": it["projects"]} for it in items],
            "amounts": [{"label": it["label"], "value": it["amount"]} for it in items],
        }
        return "\n".join(lines), chart

    def _aggregate_source(self, args: SearchDatabaseArgs, rows: list[dict]) -> dict:
        """聚合结果的统计口径条目（2026-11-15 方案 A）：数据类答案至少带一条
        诚实出处；不参与正文 [n] 编号（docs/01 §3）。"""
        scope: list[str] = []
        if args.keyword:
            scope.append(f"关键词「{args.keyword.strip()}」")
        if args.filters:
            scope.append("、".join(f"{k}={v}" for k, v in args.filters.items()))
        if args.date_range:
            dr = args.date_range
            scope.append(f"{dr.get('field', 'publish_date')}"
                         f" {dr.get('start') or '…'}~{dr.get('end') or '…'}")
        scope_text = "；".join(scope) or "全库"
        return {
            "id": f"db:agg:{args.query_type}",
            "source_type": "业务库",
            "title": f"{_AGG_LABELS[args.query_type]}（{scope_text}）",
            "text": "业务库 bidding_procurement 聚合统计：按维度分组聚合中标金额；"
                    f"返回 Top {len(rows)} 分组行，与正文表格/图表同源。",
            "url": "", "score": None,
            "meta": {"query_type": args.query_type, "groups": len(rows)},
        }

    def _detail_sources(self, rows: list[dict]) -> list[dict]:
        """明细来源条目（引用契约：正文 [n] ↔ sources 顺序）。"""
        sources = []
        for i, row in enumerate(rows):
            sources.append({
                "id": f"db:{row.get('project_no', i)}",
                "source_type": "业务库",
                "title": row.get("project_name") or row.get("project_no") or "（未命名项目）",
                "text": _format_details([row]),
                "url": row.get("source_url", ""),
                "score": None,
                "meta": {"winner": row.get("winner", ""), "region": row.get("region", ""),
                         "award_amount": row.get("award_amount"),
                         "publish_date": str(row.get("publish_date") or "")},
            })
        return sources


def _format_details(rows: list[dict]) -> str:
    lines = []
    for i, row in enumerate(rows):
        amount = row.get("award_amount")
        amount_text = f"{int(amount):,} 元" if amount is not None else "金额不可用"
        lines.append(
            f"[{i + 1}] {row.get('project_name', '')}（编号 {row.get('project_no', '')}）\n"
            f"    中标人: {row.get('winner') or '不可用'}；中标金额: {amount_text}；"
            f"地区: {row.get('region') or '不可用'}；发布: {row.get('publish_date') or '不可用'}"
        )
    return "\n".join(lines)
