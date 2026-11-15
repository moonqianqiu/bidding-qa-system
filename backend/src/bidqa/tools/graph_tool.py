"""D2 search_graph：图谱查询工具（docs/02 D2；7 类枚举引用 §4.2 模板）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bidqa.database.neo4j import GRAPH_QUERY_TYPES, NeoClient
from bidqa.tools.args_norm import normalize_choice
from bidqa.tools.base import BaseTool, empty_result, tool_result

# query_type 中文别名（与 db 工具枚举表部分同名但语义不同——图谱口径；
# 别名按图谱七类给足）
_QUERY_ALIASES = {
    "供应商": "supplier_topn", "供应商TopN": "supplier_topn", "供应商分布": "supplier_topn",
    "采购人": "purchaser_dist", "采购人分布": "purchaser_dist",
    "标的物": "subject_freq", "标的物频次": "subject_freq",
    "标的物供应商": "subject_supplier", "标的物供应商分布": "subject_supplier",
    "地区": "region_dist", "地区分布": "region_dist",
    "金额": "amount_agg", "均价": "amount_agg", "金额分布": "amount_agg",
    "趋势": "time_trend", "时间趋势": "time_trend",
}


class SearchGraphArgs(BaseModel):
    # 多余参数一律拒绝：extra=ignore 会让跨工具参数串味被静默吞掉（如把
    # keyword 传给图谱 → 静默丢参返回全库榜，2026-11-15 审计 P2）
    model_config = ConfigDict(extra="forbid")

    query_type: str = Field(description=f"图谱查询类型枚举：{'/'.join(GRAPH_QUERY_TYPES)}")
    entity: str | None = Field(default=None, max_length=100, description="实体名过滤（包含匹配）")
    top_n: int | None = Field(default=None, ge=1, le=100)

    @field_validator("query_type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        return normalize_choice(v, GRAPH_QUERY_TYPES, _QUERY_ALIASES)

    @model_validator(mode="after")
    def _subject_supplier_needs_entity(self) -> "SearchGraphArgs":
        # 与 neo4j.query 层同守卫（沿用 args+底层的双层校验模式，如 top_n）：
        # args 层报错让模型零往返自纠，底层校验守住 ingest/cli 等其他调用方
        if self.query_type == "subject_supplier" and not (self.entity or "").strip():
            raise ValueError("subject_supplier 须提供 entity（标的物片段）；"
                             "按供应商名找公司用 supplier_topn")
        return self


class SearchGraphTool(BaseTool[SearchGraphArgs]):
    name = "search_graph"
    description = ("查询项目关系图谱（Neo4j，实体归一后口径）的**实体维度分布**：标的物为 X 的"
                   "供应商排名（subject_supplier，entity 传标的物片段）、按供应商名的中标分布、"
                   "采购人/地区分布、标的物频次。排名统计类问题优先 search_database"
                   "（业务库明细更全）。金额单位=元。")
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
        return tool_result(
            text="\n".join(lines), charts=[chart],
            sources=[{
                # 聚合结果的统计口径条目（2026-11-15 方案 A）：不参与正文 [n] 编号
                "id": f"graph:agg:{args.query_type}",
                "source_type": "图谱",
                "title": ("图谱统计：" + args.query_type
                          + (f"（{args.entity}）" if args.entity else "")),
                "text": "Neo4j 实体网络聚合口径（实体归一后）；与正文文本/图表同源。",
                "url": "", "score": None,
                "meta": {"query_type": args.query_type, "groups": len(rows)},
            }])
