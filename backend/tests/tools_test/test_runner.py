"""D7 ToolRunner 契约测试（docs/02 D7：5 条路径 + 并行性 + 工具表同源）。"""

from __future__ import annotations

import threading
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
    reg = make_registry()
    runner = ToolRunner()
    [out] = runner.run_batch([ToolCall("echo", {"text": "你好"})], reg)
    assert out.ok and out.payload["text"] == "echo: 你好"
    assert out.payload["empty"] is False
    assert out.elapsed_ms >= 0


def test_path_exception_isolated() -> None:
    """单工具异常不影响同批其他工具（隔离性）。"""
    reg = make_registry()
    runner = ToolRunner()
    outs = runner.run_batch([ToolCall("boom", {"text": "x"}), ToolCall("echo", {"text": "y"})], reg)
    assert outs[0].ok is False and "RuntimeError" in outs[0].payload["error"]
    assert outs[0].payload["tool"] == "boom"
    assert outs[1].ok and outs[1].payload["text"] == "echo: y"


def test_path_timeout_fixed_format() -> None:
    """超时固定格式 {"error":"timeout","tool":…}（C3 让模型可决策重试/放弃）。"""
    reg = make_registry()
    runner = ToolRunner(batch_timeout=0.3)
    [out] = runner.run_batch([ToolCall("slow", {"text": "x"})], reg)
    assert out.ok is False
    assert out.payload == {"error": "timeout", "tool": "slow"}


def test_path_unknown_tool() -> None:
    reg = make_registry()
    runner = ToolRunner()
    [out] = runner.run_batch([ToolCall("nope", {})], reg)
    assert out.ok is False and out.payload["error"] == "unknown_tool"
    assert out.payload["tool"] == "nope"


def test_path_bad_arguments() -> None:
    reg = make_registry()
    runner = ToolRunner()
    [out] = runner.run_batch([ToolCall("echo", {"text": ""})], reg)  # 违反 min_length
    assert out.ok is False and out.payload["error"] == "bad_arguments"
    # 自纠闭环最后一跳：detail 必须携带 pydantic 错误文案（模型据此修正参数，
    # 2026-11-15 审计 P3-8——只回 error 码不回 detail 时此断言能红）
    assert "min_length" in str(out.payload.get("detail"))


def test_parallel_batch_wall_clock() -> None:
    """一轮内多工具并行：两个 1.2s 任务整批 <2s（串行则 ≥2.4s）。"""
    reg = make_registry()
    runner = ToolRunner()
    t0 = time.perf_counter()
    outs = runner.run_batch([ToolCall("slow", {"text": "a"}), ToolCall("slow", {"text": "b"})], reg)
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
    runner = ToolRunner()
    out = runner.run_batch([ToolCall("search_knowledge", {"text": "q"})], reg)[0]
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


def test_run_batch_registry_is_per_call() -> None:
    """P0-1 契约：执行期注册表按批显式传入，Runner 不持有可变注册表——
    进程内单例 Runner 被多个调度方交替使用互不串味（原实现 loop.run 首行
    无条件覆盖共享 registry：并发请求互相 unknown_tool/串模型槽位/绕过联网开关）。"""
    reg_a, reg_b = ToolRegistry(), ToolRegistry()
    reg_a.register(EchoTool())
    runner = ToolRunner()
    assert runner.run_batch([ToolCall("echo", {"text": "a"})], reg_a)[0].ok
    [cross] = runner.run_batch([ToolCall("echo", {"text": "a"})], reg_b)  # 空表：不可见
    assert cross.ok is False and cross.payload["error"] == "unknown_tool"  # 各用各的表


def test_runner_has_no_mutable_registry_attribute() -> None:
    """P0-1 结构守卫：Runner 不再持有 .registry 属性（可变共享态的根）。"""
    assert not hasattr(ToolRunner(), "registry")


def test_runner_threads_are_daemon() -> None:
    """P1-7（审计）：执行器 worker 曾非守护（3.9+ ThreadPoolExecutor 退出时 join）
    ——卡住的工具拖住整个进程退出。守护化后实测 daemon=True。"""
    reg = make_registry()
    runner = ToolRunner()
    runner.run_batch([ToolCall("echo", {"text": "x"})], reg)
    workers = [t for t in threading.enumerate() if t.name.startswith("toolrun")]
    assert workers, "worker 线程未启动"
    assert all(t.daemon for t in workers)


def test_timeout_cancels_queued_calls() -> None:
    """P1-7（审计）：超时曾只记 timeout outcome、排队任务照跑（future 不取消）。
    收口后未开跑的排队任务被取消——不再白烧执行与下游调用。"""
    executed: list[str] = []

    class CountingSlow(BaseTool[EchoArgs]):
        name = "slow"
        description = "计数慢工具（测试用）"
        args_model = EchoArgs

        def run(self, args: EchoArgs) -> dict:
            executed.append(args.text)
            time.sleep(0.6)
            return tool_result(text="done")

    reg = ToolRegistry()
    reg.register(CountingSlow())
    runner = ToolRunner(max_workers=4, batch_timeout=0.2)
    outs = runner.run_batch([ToolCall("slow", {"text": f"j{i}"}) for i in range(5)], reg)
    assert all(o.ok is False and o.payload["error"] == "timeout" for o in outs)
    time.sleep(1.0)  # 给运行中的 4 个自然跑完的时间
    assert len(executed) == 4  # 第 5 个在排队被取消，绝不执行（原=5，红）


def test_close_drains_queued_and_is_nonblocking() -> None:
    """P1-7：close()（停机钩子调用）排空排队任务且不等运行中的卡住工具——
    守护线程语义下进程退出不被拖住。run_batch 是同步阻塞语义，占满/排队须用线程。"""
    executed: list[str] = []
    started = threading.Event()
    gate = threading.Event()

    class GateTool(BaseTool[EchoArgs]):
        name = "gate"
        description = "门闩工具（测试用）"
        args_model = EchoArgs

        def run(self, args: EchoArgs) -> dict:
            executed.append(args.text)
            started.set()
            gate.wait(3.0)
            return tool_result(text="done")

    reg2 = ToolRegistry()
    reg2.register(GateTool())
    runner = ToolRunner(batch_timeout=30.0)
    boxes: list[list] = []

    th = threading.Thread(target=lambda: boxes.append(
        runner.run_batch([ToolCall("gate", {"text": f"w{i}"}) for i in range(4)], reg2)),
        daemon=True)
    th.start()
    assert started.wait(2.0)
    time.sleep(0.2)  # 4 个 worker 全部占满
    th2 = threading.Thread(target=lambda: boxes.append(
        runner.run_batch([ToolCall("gate", {"text": "q1"})], reg2)), daemon=True)
    th2.start()
    # 确定性同步：等 q1 真正入队（消除 close 与 submit 的竞态；白盒观察队列深度）
    deadline = time.perf_counter() + 2.0
    while runner._pool._queue.qsize() < 1 and time.perf_counter() < deadline:
        time.sleep(0.01)
    assert runner._pool._queue.qsize() == 1

    t0 = time.perf_counter()
    runner.close()
    assert time.perf_counter() - t0 < 1.0  # 不等运行中的卡住工具（守护语义）
    th2.join(2.0)  # 排队 future 被取消=done → run_batch 立即返回
    gate.set()
    th.join(3.5)
    assert sorted(executed) == ["w0", "w1", "w2", "w3"]  # 排队 q1 被排空，绝不执行（并行序不定）
    queued_outcomes = next(b for b in boxes if len(b) == 1)  # 返回顺序不保证（th2 先于 th 收尾）
    assert queued_outcomes[0].payload["error"] == "timeout"  # 被取消者=timeout 结构
