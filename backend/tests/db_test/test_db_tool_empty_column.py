"""D3 聚合空列守卫：分组列全空时整张不出图（AGENTS 硬约束 1：取不到记「不可用」，无数据不出图）。"""

from __future__ import annotations

from typing import Any

from bidqa.tools.db_tool import SearchDatabaseTool


class _StubPg:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def query_bp(self, args: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        return self._rows, '-- stub'


def test_subject_agg_all_null_labels_yields_no_chart() -> None:
    """真实库 subject_matter 全 NULL：单桶「（未知）」≠ 有数据，不许出图。"""
    tool = SearchDatabaseTool(_StubPg([{"label": None, "projects": 8489,
                                     "total_amount": 21_323_088_647}]))
    out = tool.run(tool.args_model.model_validate({"query_type": "subject_agg", "top_n": 10}))
    assert out["empty"] is True
    assert out["charts"] == []
    assert "不可用" in out["text"]
    assert "8489" not in out["text"]  # 不拿全库总量冒充该维度结果


def test_aggregate_with_real_labels_still_charts() -> None:
    tool = SearchDatabaseTool(_StubPg([
        {"label": "安徽省合肥市", "projects": 3, "total_amount": 100},
        {"label": "安徽省芜湖市", "projects": 1, "total_amount": None},
    ]))
    out = tool.run(tool.args_model.model_validate({"query_type": "region_agg"}))
    assert out["empty"] is False
    assert out["charts"][0]["items"][0]["label"] == "安徽省合肥市"
    assert out["charts"][0]["items"][1]["value"] == 1