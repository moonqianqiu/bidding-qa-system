"""聊天历史用例（docs/01 §3.1 B1 + C7 压缩）：PG 读写与游客进程内缓存。

从 chat_stream 归位（重构 C3）。问答对追加与标题兜底在此唯一实现
（清债 #4：原 _persist/_compress/游客 append 三处各写一遍）——落库失败不抛
（能力型，结构性错误进日志）；压缩在收尾事件之后执行（5s 限时，不拖慢收尾）。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Callable

from bidqa.agent.loop import LoopResult

logger = logging.getLogger("bidqa.application.chat.history")

HISTORY_TURNS = 6  # 进上下文的最近轮数（摘要之外的滚动窗口）


def append_exchange(items: list[dict], question: str, answer: str) -> list[dict]:
    """问答对追加（唯一实现；PG 落库与游客内存历史共用）。"""
    return items + [{"role": "user", "content": question},
                    {"role": "assistant", "content": answer}]


def title_of(row: dict | None, question: str) -> str:
    """会话标题：已有标题优先，否则取问题前 30 字（唯一实现）。"""
    return (row or {}).get("title") or question[:30]


def load_history(store: Any, conversation_id: str | None) -> tuple[str | None, list[dict], str]:
    """加载会话（最近 N 轮 + 滚动摘要）；无 PG/无会话返回 (None, [], "")。"""
    if not store or not conversation_id:
        return conversation_id, [], ""
    try:
        row = store.get_conversation(conversation_id)
    except Exception:
        return conversation_id, [], ""
    if not row:
        return conversation_id, [], ""
    messages = row.get("messages") or {}
    items = [m for m in messages.get("items", [])
             if m.get("role") in ("user", "assistant")][-HISTORY_TURNS:]
    return conversation_id, items, str(messages.get("summary", "") or "")


def persist_result(store: Any, conversation_id: str | None, question: str,
                   result: LoopResult, provider: str, prior_summary: str,
                   user_id: str | None = None) -> str:
    """会话落库（messages JSONB 内含 schema_v）；失败不抛（能力型）。
    user_id=归属（docs/01 §3.1 会话隔离）：机器主 token/legacy 为 None（机器域）。"""
    if not store:
        return conversation_id or ""
    conv_id = conversation_id or str(uuid.uuid4())
    try:
        row = store.get_conversation(conv_id)
        items = (row.get("messages") or {}).get("items", []) if row else []
        title = title_of(row, question)
        store.upsert_conversation(
            conv_id, title,
            {"schema_v": 1, "items": append_exchange(items, question,
                                                     result.answer)[-200:],
             "summary": prior_summary}, provider, user_id=user_id)
        result.conversation_id = conv_id
    except Exception as exc:
        # 落库失败不再纯静默：类型+简述进日志（脱敏原则：只记结构性错误信息）
        logger.warning("会话落库失败 conv=%s: %s: %s",
                       conv_id[:8], type(exc).__name__, str(exc)[:160])
    return conv_id


def compress_after_done(store: Any, conv_id: str, chat: Any, question: str,
                        result: LoopResult, prior_summary: str,
                        history_items: list[dict]) -> None:
    """C7 压缩：在收尾事件之后执行（5s 限时，调用方负责限时），不拖慢收尾；
    失败=等同不开启（契约）。"""
    if store is None or chat is None:
        return
    try:
        from bidqa.agent.compress import compress_history
        summary = compress_history(
            chat, append_exchange(history_items, question, result.answer),
            prior_summary=prior_summary, conversation_id=conv_id)
        if summary:
            row2 = store.get_conversation(conv_id)
            items2 = (row2.get("messages") or {}).get("items", [])
            store.upsert_conversation(conv_id, title_of(row2, question),
                                      {"schema_v": 1, "items": items2,
                                       "summary": summary}, "")
    except Exception as exc:  # 压缩失败不影响问答，但要在日志留痕（v2.2：不再全静默）
        logger.warning("会话压缩失败 conv=%s: %s: %s", conv_id,
                       type(exc).__name__, str(exc)[:120])


def defer_compress(store: Any, conv_id: str, chat: Any, question: str,
                   result: LoopResult, prior_summary: str,
                   history_items: list[dict]) -> Callable[[], None]:
    """压缩延后闭包：编排层先投递收尾事件，再执行本闭包（保持原时序）。"""
    return lambda: compress_after_done(store, conv_id, chat, question, result,
                                       prior_summary, history_items)


class GuestHistory:
    """游客会话的进程内历史（docs/01 §3.1 游客语义：不落库，多轮上下文仍可用）。

    TTL 1h、封顶 HISTORY_TURNS 轮、条目数有界、重启即失——与「关页即失」一致的
    诚实内存态（workers=1 允许进程内状态）。admin/user 会话不走这里（落 PG）。
    """

    TTL_SECONDS = 3600.0
    MAX_CONVERSATIONS = 256

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, list[dict]]] = {}
        self._lock = threading.Lock()

    def load(self, conv_id: str | None) -> list[dict]:
        if not conv_id:
            return []
        with self._lock:
            entry = self._data.get(conv_id)
            if entry is None:
                return []
            ts, items = entry
            if time.monotonic() - ts > self.TTL_SECONDS:
                self._data.pop(conv_id, None)
                return []
            return list(items)

    def append(self, conv_id: str | None, question: str, answer: str) -> None:
        if not conv_id:
            return
        with self._lock:
            now = time.monotonic()
            ts, items = self._data.get(conv_id, (now, []))
            items = append_exchange(items, question, answer)[-HISTORY_TURNS:]
            self._data[conv_id] = (now, items)
            if len(self._data) > self.MAX_CONVERSATIONS:  # 有界防泄漏：丢最旧一半
                for key in sorted(self._data, key=lambda k: self._data[k][0])[
                        :self.MAX_CONVERSATIONS // 2]:
                    self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


guest_history = GuestHistory()
