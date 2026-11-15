"""D7 ToolRunner 契约测试（docs/02 D7：5 条路径 + 并行性 + 工具表同源）。"""

from __future__ import annotations

import time
from typing import Any

import pytest
from pydantic import BaseModel, Field

from bidqa.tools.base import BaseTool, ToolRegistry, empty_result, tool_result
from bidqa.tools.runner import ToolCall, ToolRunner, quality_note


class EchoArgs(BaseModel):
    text: str = Field(min_length=1, max_length=100)


class EchoTool(BaseTool[EchoArgs]):
    name = "echo"
    description = "回显文本（测试用）"
    args_model = EchoArgs

    def run(self, args: EchoArgs) -> dict[str, Any]:
        return tool_result(text=f"echo: {args.text}")


class BoomTool(BaseTool[EchoArgs]):
    name = "boom"
    description = "必炸（测试用）"
    args_model = EchoArgs

    def run(self, args: EchoArgs) -> dict[str, Any]:
        raise RuntimeError("工具内部故障")


class SlowTool(BaseTool[EchoArgs]):
    name = "slow"
    description = "慢速（测试用）"
    args_model = EchoArgs

    def run(self, args: EchoArgs) -> dict[str, Any]:
        time.sleep(1.2)
        return tool_result(text="done")


def make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(EchoTool())
    reg.register(BoomTool())
    reg.register(SlowTool())
    return reg


def test_path_success() -> None:
    runner = ToolRunner(make_registry())
    [out] = runner.run_batch([ToolCall("echo", {"text": "你好"})])
    assert out.ok and out.payload["text"] == "echo: 你好"
    assert out.payload["empty"] is False
    assert out.elapsed_ms >= 0


def test_path_exception_isolated() -> None:
    """单工具异常不影响同批其他工具（隔离性）。"""
    runner = ToolRunner(make_registry())
    outs = runner.run_batch([ToolCall("boom", {"text": "x"}), ToolCall("echo", {"text": "y"})])
    assert outs[0].ok is False and "RuntimeError" in outs[0].payload["error"]
    assert outs[0].payload["tool"] == "boom"
    assert outs[1].ok and outs[1].payload["text"] == "echo: y"


def test_path_timeout_fixed_format() -> None:
    """超时固定格式 {"error":"timeout","tool":…}（C3 让模型可决策重试/放弃）。"""
    runner = ToolRunner(make_registry(), batch_timeout=0.3)
    [out] = runner.run_batch([ToolCall("slow", {"text": "x"})])
    assert out.ok is False
    assert out.payload == {"error": "timeout", "tool": "slow"}


def test_path_unknown_tool() -> None:
    runner = ToolRunner(make_registry())
    [out] = runner.run_batch([ToolCall("nope", {})])
    assert out.ok is False and out.payload["error"] == "unknown_tool"
    assert out.payload["tool"] == "nope"


def test_path_bad_arguments() -> None:
    runner = ToolRunner(make_registry())
    [out] = runner.run_batch([ToolCall("echo", {"text": ""})])  # 违反 min_length
    assert out.ok is False and out.payload["error"] == "bad_arguments"
    # 自纠闭环最后一跳：detail 必须携带 pydantic 错误文案（模型据此修正参数，
    # 2026-11-15 审计 P3-8——只回 error 码不回 detail 时此断言能红）
    assert "min_length" in str(out.payload.get("detail"))


def test_parallel_batch_wall_clock() -> None:
    """一轮内多工具并行：两个 1.2s 任务整批 <2s（串行则 ≥2.4s）。"""
    runner = ToolRunner(make_registry())
    t0 = time.perf_counter()
    outs = runner.run_batch([ToolCall("slow", {"text": "a"}), ToolCall("slow", {"text": "b"})])
    wall = time.perf_counter() - t0
    assert all(out.ok for out in outs)
    assert wall < 2.2


def test_empty_result_structure() -> None:
    """空结果统一「不可用」结构（D 域统一约定）。"""
    empty = empty_result()
    assert empty == {"text": empty_result()["text"], "sources": [], "charts": [], "empty": True}
    assert "放宽限定条件" in empty["text"]


def test_registry_prompt_table_and_schema_same_source() -> None:
    """C2 同源：openai_schemas 与 prompt_table 都出自注册表。"""
    reg = make_registry()
    schemas = reg.openai_schemas()
    assert [s["function"]["name"] for s in schemas] == ["echo", "boom", "slow"]
    echo_schema = schemas[0]["function"]["parameters"]
    assert echo_schema["properties"]["text"]["minLength"] == 1
    table = reg.prompt_table()
    assert "- echo(text): 回显文本（测试用）" in table
    assert "slow" in table

    with pytest.raises(ValueError):  # 重复注册拒绝
        reg.register(EchoTool())


# ── 检索质量标签（参考吸收 #4a）：✅/⚠️ 注入 tool 消息供模型决策补查 ──

class ScoreTool(BaseTool[EchoArgs]):
    name = "score"
    description = "带相关度来源的假检索（测试用）"
    args_model = EchoArgs

    def __init__(self, scores: list[float]):
        self.scores = scores

    def run(self, args: EchoArgs) -> dict[str, Any]:
        return tool_result(text=f"资料: {args.text}",
                           sources=[{"title": f"t{i}", "text": "x", "score": s}
                                    for i, s in enumerate(self.scores)])


def test_quality_note_knowledge_threshold() -> None:
    hi = quality_note("search_knowledge", {"sources": [{"score": 0.8}, {"score": 0.6}]})
    assert hi.startswith("（✅ 检索质量良好：2 条，均分 0.70）")
    lo = quality_note("search_knowledge", {"sources": [{"score": 0.3}, {"score": 0.4}]})
    assert lo.startswith("（⚠️ 相关性偏低：2 条，均分 0.35——")
    assert "换关键词重新检索" in lo


def test_quality_note_charts_and_skip_paths() -> None:
    assert quality_note("search_database", {"charts": [{}, {}]}).startswith("（✅ 数据查询成功：2 张")
    assert quality_note("search_database", {"empty": True}) == ""  # 空结果已有 EMPTY_TEXT 话术
    assert quality_note("search_database", {"error": "timeout"}) == ""  # 失败已有结构化错误
    assert quality_note("echo", {"text": "无分数无图表"}) == ""  # 无评分来源/图表不贴标签


def test_run_batch_appends_note_to_text() -> None:
    class KnowledgeScoreTool(ScoreTool):
        name = "search_knowledge"  # 质量标签按工具名路由：知识库才贴均分标签

    reg = ToolRegistry()
    reg.register(KnowledgeScoreTool([0.2, 0.3]))
    runner = ToolRunner(reg)
    out = runner.run_batch([ToolCall("search_knowledge", {"text": "q"})])[0]
    assert out.ok
    assert out.payload["text"].endswith(
        "（⚠️ 相关性偏低：2 条，均分 0.25——材料与问题不符时建议换关键词重新检索）")


def test_quality_note_skips_when_scores_none() -> None:
    """score=None（RRF 分/点积分不冒充相关度）不贴标签，绝不把 None 当 0。"""
    assert quality_note("search_knowledge",
                        {"sources": [{"score": None}, {"score": None}]}) == ""
    assert quality_note("search_knowledge",
                        {"sources": [{"score": None}, {"score": 0.8}]}).startswith(
        "（✅ 检索质量良好：1 条")
