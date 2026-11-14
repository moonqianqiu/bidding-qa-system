"""ToolRunner 并行执行器（docs/02 D7；C3 对齐）。

约定（docs/01 §2 tools 行 + §6 单进程）：
- 一轮内多工具并行（线程池，守护线程）；整批共享 30s 墙钟超时；
- 未知工具 / 坏参数 / 工具异常 / 超时 一律返回 tool 消息结构，绝不向循环抛异常；
- 超时固定格式 {"error":"timeout","tool":"<name>"}——让模型下一轮可决策重试/放弃
  （直接影响 hit_cap 率）。
"""

from __future__ import annotations

import json
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from bidqa.tools.base import ToolRegistry

BATCH_TIMEOUT = 30.0  # 整批共享墙钟（docs/02 C3）
MAX_WORKERS = 4       # 队列容量即并发上限（docs/01 §2 后台任务约定）


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolOutcome:
    """与 ToolCall 等长对齐的结果序列元素：ok=False 时 error 结构进对话。"""

    call: ToolCall
    payload: dict[str, Any]
    ok: bool
    elapsed_ms: int

    def as_tool_message(self, tool_call_id: str = "") -> dict[str, str]:
        return {"role": "tool", "tool_call_id": tool_call_id, "name": self.call.name,
                "content": json.dumps(self.payload, ensure_ascii=False, default=str)}


def quality_note(name: str, payload: dict[str, Any]) -> str:
    """检索质量标签（参考项目实证做法，算法 §25 同款）：附在 tool 消息里供模型
    决策「是否需要下一轮补查」——hit_cap 的护栏。✅/⚠️ 只进 tool 消息不进用户正文；
    失败/空结果已有结构化话术（EMPTY_TEXT/error），不重复贴标签。

    sources[].score 为 None（RRF 排名分 / bm25 点积分不冒充相关度，见 retrieve.py）
    时收不到数值，不贴标签——避免「⚠️ 相关性偏低」误诱导模型反复换词，
    绝不把 None 当 0 记。"""
    if payload.get("error") or payload.get("empty"):
        return ""
    sources = payload.get("sources") or []
    scores = [s.get("score") for s in sources if isinstance(s.get("score"), (int, float))]
    if name == "search_knowledge" and scores:
        avg = sum(scores) / len(scores)
        if avg >= 0.5:
            return f"（✅ 检索质量良好：{len(scores)} 条，均分 {avg:.2f}）"
        return (f"（⚠️ 相关性偏低：{len(scores)} 条，均分 {avg:.2f}"
                "——材料与问题不符时建议换关键词重新检索）")
    charts = payload.get("charts") or []
    if charts:
        return f"（✅ 数据查询成功：{len(charts)} 张图表可渲染）"
    return ""


class ToolRunner:
    def __init__(self, registry: ToolRegistry, batch_timeout: float = BATCH_TIMEOUT,
                 max_workers: int = MAX_WORKERS):
        self.registry = registry
        self.batch_timeout = batch_timeout
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="toolrun")

    def run_batch(self, calls: list[ToolCall]) -> list[ToolOutcome]:
        """并行执行一批调用；返回顺序与 calls 一致；任何路径都不抛异常。"""
        if not calls:
            return []

        futures: dict[Future, ToolCall] = {}
        for call in calls:
            futures[self._pool.submit(self._execute, call)] = call

        done, _ = wait(list(futures), timeout=self.batch_timeout)
        results: list[ToolOutcome] = []
        for fut, call in futures.items():
            if fut in done:
                _, payload, ok, ms = fut.result()
                results.append(ToolOutcome(call=call, payload=payload, ok=ok, elapsed_ms=ms))
            else:  # 整批墙钟已到：未完成一律记超时（线程为守护，进程退出不阻塞）
                results.append(ToolOutcome(call=call, payload={"error": "timeout", "tool": call.name},
                                           ok=False, elapsed_ms=-1))
        return results

    # ── 单工具执行（隔离：所有异常在这里消化）──
    def _execute(self, call: ToolCall) -> tuple[ToolCall, dict[str, Any], bool, int]:
        t0 = time.perf_counter()
        try:
            tool = self.registry.get(call.name)
            if tool is None:
                return call, {"error": "unknown_tool", "tool": call.name,
                              "detail": f"未注册工具: {call.name}"}, False, self._ms(t0)
            try:
                args = tool.validate(call.arguments)
            except ValidationError as exc:
                return call, {"error": "bad_arguments", "tool": call.name,
                              "detail": str(exc.errors()[:3])}, False, self._ms(t0)
            payload = tool.run(args)
            note = quality_note(call.name, payload)
            if note:
                payload = {**payload, "text": f"{payload.get('text', '')}\n\n{note}"}
            return call, payload, True, self._ms(t0)
        except Exception as exc:  # 单工具挂掉不拖垮整批（docs/01 §2 agent 行）
            return call, {"error": f"{type(exc).__name__}: {exc}", "tool": call.name}, False, self._ms(t0)

    @staticmethod
    def _ms(t0: float) -> int:
        return int((time.perf_counter() - t0) * 1000)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
