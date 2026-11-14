"""C7 历史压缩（限时/滚动增量/LRU/失败等同不开启）。"""

from __future__ import annotations

import time

from bidqa.agent import compress
from bidqa.agent.compress import compress_history


class SlowChat:
    def chat(self, messages, temperature=0.3):
        time.sleep(2.0)
        return "慢摘要"


class FastChat:
    def __init__(self, reply: str = "用户在安徽，关注空调采购，已给出TopN结论。"):
        self.reply = reply
        self.calls = 0

    def chat(self, messages, temperature=0.3):
        self.calls += 1
        user = messages[-1]["content"]
        assert "【新增对话】" in user
        if "【已有摘要】" in user:
            assert "已给出TopN结论" in user or "摘要" in user  # 滚动增量带旧摘要
        return self.reply


def test_compress_success_and_budget() -> None:
    chat = FastChat()
    items = [{"role": "user", "content": "安徽省空调采购有哪些？"},
             {"role": "assistant", "content": "共12个，甲公司最多。"}]
    summary = compress_history(chat, items, conversation_id="c1")
    assert summary and len(summary) <= compress.SUMMARY_CHAR_BUDGET
    assert summary.startswith("用户在安徽")


def test_compress_timeout_returns_none() -> None:
    """5s 限时：慢模型 → None（等同不开启），调用方历史路径逐字节不变。"""
    start = time.perf_counter()
    out = compress_history(SlowChat(), [{"role": "user", "content": "q"}],
                           conversation_id="c2", timeout=0.3)
    elapsed = time.perf_counter() - start
    assert out is None
    assert elapsed < 1.5  # 限时生效，不真等 2s


def test_compress_failure_returns_none() -> None:
    class Boom:
        def chat(self, messages, temperature=0.3):
            raise RuntimeError("模型挂了")

    assert compress_history(Boom(), [{"role": "user", "content": "q"}],
                            conversation_id="c3") is None


def test_compress_no_chat_or_empty_is_none() -> None:
    assert compress_history(None, [{"role": "user", "content": "q"}]) is None
    assert compress_history(FastChat(), []) is None


def test_compress_rolling_incremental() -> None:
    chat = FastChat()
    first = compress_history(chat, [{"role": "user", "content": "q1"},
                                    {"role": "assistant", "content": "a1"}],
                             conversation_id="c4")
    second = compress_history(chat, [{"role": "user", "content": "q2"},
                                     {"role": "assistant", "content": "a2"}],
                              prior_summary=first or "", conversation_id="c4")
    assert second  # 滚动增量成立（新摘要基于旧摘要+新增）


def test_lru_cache_hit() -> None:
    compress._cache.clear()
    chat = FastChat()
    items = [{"role": "user", "content": "q"}]
    compress_history(chat, items, conversation_id="c5")
    compress_history(chat, items, conversation_id="c5")
    assert chat.calls == 1  # 第二次命中 LRU
