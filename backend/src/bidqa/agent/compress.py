"""C7 历史压缩（docs/02 C7）。

触发时机：每轮收尾后前台同步执行但**限时 5s**（超时本轮不压缩，不拖慢下一问首 token）；
摘要预算 ≤500 token（按 1 token≈1.5 汉字≈750 字符封顶）；滚动增量（已摘要永不重送，
新历史只在摘要之上增量压缩）；LRU 缓存；**任何失败退回「等同不开启」**——
调用方拿到 None 时与不启用压缩的历史路径逐字节一致。
单进程约定（docs/01 §6）。
"""

from __future__ import annotations

import hashlib
import json
import queue
import re
import threading
from concurrent.futures import Future
from typing import Any

COMPRESS_TIMEOUT = 5.0
SUMMARY_CHAR_BUDGET = 750  # ≈500 token（中文 1 token≈1.5 字符）
LRU_SIZE = 64

_COMPRESS_PROMPT = (
    "你是对话历史压缩器。把【已有摘要】与【新增对话】合并为一段滚动摘要：\n"
    "①保留用户已确认的限定条件（地区/时间/标的物）②保留已给出的关键结论与数字"
    "③保留未决问题 ④不超过 750 字。只输出摘要正文。"
)

_cache: dict[str, str] = {}  # LRU：key = 内容寻址 sha256（P1-11）
_dropped = 0  # 队列满即丢计数（P1-7：满即丢并计数，绝不无界积压）


class _CompressWorker:
    """单条守护 worker（审计 P1-7：原 ThreadPoolExecutor 线程非守护且提交无界）。
    队列容量 1：上一轮压缩未完时新提交入队，再满即丢（等同不开启，计数可查）。"""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue(maxsize=1)
        threading.Thread(target=self._loop, daemon=True, name="compress").start()

    def submit(self, fn, *args) -> Future | None:
        fut: Future = Future()
        try:
            self._queue.put_nowait((fn, args, fut))
        except queue.Full:
            global _dropped
            _dropped += 1
            return None  # 等同不开启（诚实降级，绝不积压烧钱）
        return fut

    def _loop(self) -> None:
        while True:
            fn, args, fut = self._queue.get()
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn(*args))
            except BaseException as exc:  # Future 容器承载，不炸 worker
                fut.set_exception(exc)

    def close(self) -> None:
        while True:
            try:
                _fn, _args, fut = self._queue.get_nowait()
            except queue.Empty:
                return
            fut.cancel()


_worker: _CompressWorker | None = None


def _get_worker() -> _CompressWorker:
    global _worker
    if _worker is None:
        _worker = _CompressWorker()
    return _worker


def dropped_count() -> int:
    """队列满即丢计数（可观测性：metrics/测试用）。"""
    return _dropped


def close() -> None:
    """停机钩子（lifespan 调用）：排空排队任务，不等运行中的卡住压缩。"""
    if _worker is not None:
        _worker.close()


def _cache_get(key: str) -> str | None:
    if key in _cache:
        value = _cache.pop(key)
        _cache[key] = value
        return value
    return None


def _cache_put(key: str, value: str) -> None:
    _cache[key] = value
    if len(_cache) > LRU_SIZE:
        _cache.pop(next(iter(_cache)))


def _clip(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:SUMMARY_CHAR_BUDGET]


def _compress_sync(chat: Any, prior_summary: str, new_items: list[dict]) -> str:
    """同步压缩调用（在线程池里跑，外层限时）。"""
    lines = [f"{m['role']}: {m['content'][:300]}" for m in new_items[-12:]]
    parts = [f"【已有摘要】{prior_summary}"] if prior_summary else []
    parts.append("【新增对话】\n" + "\n".join(lines))
    raw = str(chat.chat([
        {"role": "system", "content": _COMPRESS_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ], temperature=0.1))
    return _clip(raw)


def compress_history(chat: Any, items: list[dict], prior_summary: str = "",
                     conversation_id: str = "", timeout: float = COMPRESS_TIMEOUT) -> str | None:
    """压缩入口。返回新摘要；任何失败/超时/不可用 → None（等同不开启）。

    缓存键内容寻址（审计 P1-11：原 (conversation_id, len(items)) 在历史窗口恒定后
    失效——第 4 轮起 len 不再变，摘要永久冻结）：输入=items 全文 + prior_summary。
    """
    if not items or chat is None:
        return None
    payload = json.dumps({"s": prior_summary,
                          "i": [(m.get("role"), m.get("content")) for m in items]},
                         ensure_ascii=False)
    key = f"{conversation_id}|{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"
    if cached := _cache_get(key):
        return cached
    future = _get_worker().submit(_compress_sync, chat, prior_summary, items)
    if future is None:
        return None  # 队列满（上一轮未跑完）→ 本轮等同不开启（P1-7 满即丢）
    try:
        summary = future.result(timeout=timeout)
    except Exception:  # 超时/异常：本轮不压缩（future 留给守护线程自然结束）
        return None
    if not summary:
        return None
    _cache_put(key, summary)
    return summary
