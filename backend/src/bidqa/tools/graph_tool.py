"""D2 search_graph：图谱查询工具（docs/02 D2；6 类枚举引用 §4.2 模板）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from bidqa.database.neo4j import GRAPH_QUERY_TYPES, NeoClient
from bidqa.tools.args_norm import normalize_choice
from bidqa.tools.base import BaseTool, empty_result, tool_result

# query_type 中文别名（与 db 工具枚举表不同名易混，别名按图谱六类给足）
_QUERY_ALIASES = {
    "供应商": "supplier_topn", "供应商TopN": "supplier_topn", "供应商分布": "supplier_topn",
    "采购人": "purchaser_dist", "采购人分布": "purchaser_dist",
    "标的物": "subject_freq", "标的物频次": "subject_freq",
    "地区": "region_dist", "地区分布": "region_dist",
    "金额": "amount_agg", "均价": "amount_agg", "金额分布": "amount_agg",
    "趋势": "time_trend", "时间趋势": "time_trend",
}


class SearchGraphArgs(BaseModel):
    query_type: str = Field(description=f"图谱查询类型枚举：{'/'.join(GRAPH_QUERY_TYPES)}")
    entity: str | None = Field(default=None, max_length=100, description="实体名过滤（包含匹配）")
    top_n: int | None = Field(default=None, ge=1, le=100)

    @field_validator("query_type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        return normalize_choice(v, GRAPH_QUERY_TYPES, _QUERY_ALIASES)


class SearchGraphTool(BaseTool[SearchGraphArgs]):
    name = "search_graph"
    description = ("查询项目关系图谱（Neo4j 实体网络）的**实体维度分布**：主要供应商（中标次数/"
                   "中标总额排名）、采购人画像、地区分布、标的物频次。问『主要供应商有哪些公司/"
                   "采购人都谁/供应商分布』这类实体关系问题用它。金额单位=元。")
    args_model = SearchGraphArgs

    def __init__(self, neo: NeoClient):
        self.neo = neo

    def run(self, args: SearchGraphArgs) -> dict[str, Any]:
        rows = self.neo.query(args.query_type, entity=args.entity,
                              top_n=args.top_n or 10)
        if not rows:
            return empty_result()
        lines = []
        for i, row in enumerate(rows):
            amount = row.get("total_amount")
            amount_text = f"{int(amount):,} 元" if amount is not None else "金额不可用"
            extra = ""
            if row.get("avg_amount") is not None:
                extra = f"，均价 {int(row['avg_amount']):,} 元"
            lines.append(f"[{i + 1}] {row.get('label') or '（未知）'}："
                         f"{row['projects']} 个项目，{amount_text}{extra}")
        chart = {
            "id": "", "kind": "bar",
            "title": f"图谱统计：{args.query_type}" + (f"（{args.entity}）" if args.entity else ""),
            "source": "graph", "format": "int", "max": None,
            "items": [{"label": str(r.get("label") or "（未知）"),
                       "value": int(r["projects"])} for r in rows],
        }
        return tool_result(text="\n".join(lines), charts=[chart])
