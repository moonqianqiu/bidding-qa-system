"""ToolRunner 并行执行器（docs/02 D7；C3 对齐）。

约定（docs/01 §2 tools 行 + §6 单进程）：
- 一轮内多工具并行（线程池，守护线程）；整批共享 30s 墙钟超时；
- 未知工具 / 坏参数 / 工具异常 / 超时 一律返回 tool 消息结构，绝不向循环抛异常；
- 超时固定格式 {"error":"timeout","tool":"<name>"}——让模型下一轮可决策重试/放弃
  （直接影响 hit_cap 率）。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from bidqa.tools.base import ToolRegistry

BATCH_TIMEOUT = 30.0  # 整批共享墙钟（docs/02 C3）
MAX_WORKERS = 4       # 并发上限（docs/01 §2 后台任务约定）
QUEUE_SIZE = 64       # 提交积压上限（满=该批立即失败；请求速率已被限流层收敛）


class _DaemonPool:
    """有界守护线程池（硬约束 4：后台任务用守护线程；审计 P1-7）。

    替代 ThreadPoolExecutor：3.9+ 其 worker 非守护且解释器退出时 join——
    卡住的工具会拖住整个进程退出。固定 max_workers 条 daemon worker +
    有界提交队列；Future 用 concurrent.futures.Future 容器
    （wait/cancel/超时语义与线程池一致）。close() 供停机钩子调用。"""

    def __init__(self, max_workers: int, queue_size: int = QUEUE_SIZE):
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._threads = [threading.Thread(target=self._worker, daemon=True,
                                          name=f"toolrun-{i}")
                         for i in range(max_workers)]
        for t in self._threads:
            t.start()

    def submit(self, fn, *args) -> Future:
        fut = Future()
        try:
            self._queue.put_nowait((fn, args, fut))
        except queue.Full:
            fut.set_exception(RuntimeError("toolrun queue full"))
        return fut

    def _worker(self) -> None:
        while True:
            fn, args, fut = self._queue.get()
            if not fut.set_running_or_notify_cancel():
                continue  # 已取消（超时/停机排空）
            try:
                fut.set_result(fn(*args))
            except BaseException as exc:  # Future 容器承载，不炸 worker
                fut.set_exception(exc)

    def close(self) -> None:
        """停机：排空未执行任务（取消其 Future），不等运行中的卡住工具——
        worker 为守护线程，进程退出不被拖住（审计 P1-7「close() 从未调用」）。"""
        while True:
            try:
                _fn, _args, fut = self._queue.get_nowait()
            except queue.Empty:
                return
            fut.cancel()


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
    """纯执行器（审计 P0-1 收口）：不持有注册表——执行期注册表由调度层按批
    显式传入（本会话挂载子集）。原实现构造器持有可变 registry 且 loop.run 首行
    无条件覆盖进程级单例的注册表：并发请求互相串味（unknown_tool/串模型槽位/
    绕过联网开关）。"""

    def __init__(self, batch_timeout: float = BATCH_TIMEOUT,
                 max_workers: int = MAX_WORKERS):
        self.batch_timeout = batch_timeout
        self._pool = _DaemonPool(max_workers)

    def run_batch(self, calls: list[ToolCall], registry: ToolRegistry) -> list[ToolOutcome]:
        """并行执行一批调用；registry=本会话挂载子集（P0-1/P1-9：未挂载工具
        一律 unknown_tool，联网开关/首轮约束不可被幻觉点名绕过）。
        返回顺序与 calls 一致；任何路径都不抛异常。"""
        if not calls:
            return []

        futures: dict[Future, ToolCall] = {}
        for call in calls:
            futures[self._pool.submit(self._execute, call, registry)] = call

        # 墙钟轮询（不能用 wait() 坐等：CPython 3.14 实测 cancel() 不唤醒 wait——
        # 停机 close() 取消排队 future 后 wait 会坐满超时；done() 轮询对
        # FINISHED/CANCELLED 均即时，20ms 间隔相对工具耗时是噪声级）
        deadline = time.monotonic() + self.batch_timeout
        while not all(fut.done() for fut in futures):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)

        results: list[ToolOutcome] = []
        for fut, call in futures.items():
            if fut.done() and not fut.cancelled():
                try:
                    _, payload, ok, ms = fut.result()
                except Exception:  # 提交积压满（queue full）等池层失败 → 结构化错误
                    payload, ok, ms = ({"error": "pool_overloaded", "tool": call.name},
                                       False, -1)
                results.append(ToolOutcome(call=call, payload=payload, ok=ok, elapsed_ms=ms))
            else:
                fut.cancel()  # P1-7：排队未跑的任务取消（运行中的无法杀，交给守护语义）
                results.append(ToolOutcome(call=call, payload={"error": "timeout", "tool": call.name},
                                           ok=False, elapsed_ms=-1))
        return results

    # ── 单工具执行（隔离：所有异常在这里消化）──
    def _execute(self, call: ToolCall, registry: ToolRegistry
                 ) -> tuple[ToolCall, dict[str, Any], bool, int]:
        t0 = time.perf_counter()
        try:
            tool = registry.get(call.name)
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
        """停机钩子（lifespan 调用）：排空排队任务，不等运行中的卡住工具。"""
        self._pool.close()
