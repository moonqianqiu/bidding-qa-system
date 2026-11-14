"""D3 search_database：业务库查询工具（docs/02 D3；白名单引用 01 §4.3 字段表）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from bidqa.database.postgres import QUERY_TYPES, PgClient
from bidqa.tools.base import BaseTool, empty_result, tool_result


class SearchDatabaseArgs(BaseModel):
    query_type: str = Field(description="查询类型枚举")
    keyword: str | None = Field(default=None, max_length=200, description="明细检索关键词")
    filters: dict[str, Any] | None = Field(
        default=None,
        description="白名单列过滤，键用英文列名：region / subject_matter / winner / purchaser / "
                    "agency / project_name / project_no / project_category / project_stage / "
                    "location / award_amount / batch_id（中文键如『地区』『标的物』亦可，自动映射）。"
                    "region 为片段匹配，传『安徽省』或『合肥』都能命中；其余列为等值匹配。")
    top_n: int | None = Field(default=None, ge=1, le=100)
    date_range: dict[str, Any] | None = Field(default=None, description="{field,start,end}")

    @field_validator("query_type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        if v not in QUERY_TYPES:
            raise ValueError(f"query_type 须为 {QUERY_TYPES}")
        return v


class SearchDatabaseTool(BaseTool):
    name = "search_database"
    description = ("查询招投标业务库（项目明细表）的**数值统计与明细**：各标的物/各地区项目数与金额、"
                   "TopN 排名、按月趋势、关键词明细检索。问『多少个/多少钱/前几名/明细』优先用它。"
                   "可用 region / subject_matter / winner / purchaser 等白名单列过滤（地区传省或市均可）。"
                   "金额单位=元。")
    args_model = SearchDatabaseArgs

    def __init__(self, pg: PgClient):
        self.pg = pg

    def run(self, args: SearchDatabaseArgs) -> dict[str, Any]:
        rows, _sql = self.pg.query_bp(args.model_dump(exclude_none=True))
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
        return tool_result(text=text, charts=[chart])

    # ── 聚合输出：文本 + 图表规格（docs/01 §3.7；id 由 B7 内容寻址补齐）──
    def _aggregate_output(self, query_type: str, rows: list[dict]) -> tuple[str, dict]:
        label_map = {
            "subject_agg": "按标的物统计", "region_agg": "按地区统计",
            "supplier_topn": "中标供应商TopN", "purchaser_topn": "采购人TopN",
            "time_trend": "按月趋势",
        }
        lines = []
        items = []
        for row in rows:
            label = row.get("label") or "（未知）"
            amount = row.get("total_amount")
            amount_text = f"{int(amount):,} 元" if amount is not None else "金额不可用"
            lines.append(f"- {label}：{row['projects']} 个项目，{amount_text}")
            items.append({"label": label, "projects": int(row["projects"]),
                          "amount": amount})
        title = f"{label_map[query_type]}（项目数与中标金额）"
        chart = {
            "id": "", "kind": "bar", "title": title, "source": "db",
            "format": "int", "max": None,
            "items": [{"label": it["label"], "value": it["projects"]} for it in items],
            "amounts": [{"label": it["label"], "value": it["amount"]} for it in items],
        }
        return "\n".join(lines), chart

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
