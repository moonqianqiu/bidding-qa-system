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


def test_cache_key_is_content_addressed() -> None:
    """P1-11（审计）：键曾是 (conversation_id, len(items))——历史窗口恒 6 条后
    第 4 轮起 len 不再变，摘要永久冻结（不同内容只调用 1 次模型）。改内容寻址后
    同长不同内容必须真压缩。"""
    compress._cache.clear()
    chat = FastChat()
    first = compress_history(chat, [{"role": "user", "content": "安徽空调有哪些？"},
                                    {"role": "assistant", "content": "12 个。"}],
                             prior_summary="", conversation_id="c6")
    assert first
    second = compress_history(chat, [{"role": "user", "content": "江苏电梯有哪些？"},
                                     {"role": "assistant", "content": "8 个。"}],
                              prior_summary=first, conversation_id="c6")
    assert chat.calls == 2  # 同 len 不同内容：缓存不得命中（旧键下=1，红）


def test_compress_worker_threads_are_daemon() -> None:
    """P1-7（审计）：压缩执行器 worker 曾非守护——卡住的压缩拖住进程退出。"""
    compress._cache.clear()
    compress_history(FastChat(), [{"role": "user", "content": "q"}], conversation_id="c7d")
    workers = [t for t in __import__("threading").enumerate() if t.name.startswith("compress")]
    assert workers and all(t.daemon for t in workers)


def test_compress_queue_full_drops_with_count() -> None:
    """P1-7（审计）：压缩提交曾无界排队；收口后队列容量 1——满即丢并计数
    （等同不开启的诚实降级）。compress_history 是同步阻塞语义，排队必须用
    线程并发提交才能复现满队列。"""
    import threading as _threading

    compress._cache.clear()
    gate = _threading.Event()
    started = _threading.Event()
    results: list = []

    class GateChat:
        def chat(self, messages, temperature=0.3):
            started.set()
            gate.wait(3.0)
            return "门闩摘要"

    items = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    t1 = _threading.Thread(target=lambda: compress_history(GateChat(), items,
                                                          conversation_id="c7f1"),
                           daemon=True)
    t1.start()
    assert started.wait(2.0)  # 第一个已占用 worker（queue 空）
    t2 = _threading.Thread(target=lambda: results.append(
        compress_history(FastChat(), items, conversation_id="c7f2")), daemon=True)
    t2.start()
    time.sleep(0.2)  # t2 已提交并排队（容量 1 占满）
    dropped = compress_history(FastChat(), items, conversation_id="c7f3")  # 满即丢
    assert dropped is None  # 等同不开启
    assert compress.dropped_count() == 1
    gate.set()
    t1.join(3.0)
    t2.join(3.0)
    assert results and results[0]  # 排队那个最终会跑（下一轮的压缩不丢）


def test_compress_after_done_preserves_provider_and_owner() -> None:
    """P1-11（审计）+ P1-1 守卫配套：二次 upsert 曾用 provider="" 覆盖（命名档
    选择丢失）且不带 user_id——owner 守卫下整条更新会被拒绝。"""
    from bidqa.agent.loop import LoopResult
    from bidqa.application.chat.history import compress_after_done

    class FakeStore:
        def __init__(self):
            self.row = {"id": "c9", "title": "原标题", "provider": "deep",
                        "user_id": "u9",
                        "messages": {"schema_v": 2, "items": [
                            {"role": "user", "content": "q"},
                            {"role": "assistant", "content": "a"}]}}
            self.upserts: list[dict] = []

        def get_conversation(self, conv_id):
            return self.row

        def upsert_conversation(self, conv_id, title, messages, provider, user_id=None):
            self.upserts.append({"title": title, "messages": messages,
                                 "provider": provider, "user_id": user_id})
            self.row = {**self.row, "title": title, "messages": messages,
                        "provider": provider, "user_id": user_id}

    compress._cache.clear()
    store = FakeStore()
    chat = FastChat()
    compress_after_done(store, "c9", chat, "q", LoopResult(answer="a"),
                        prior_summary="", history_items=[], user_id="u9")
    assert chat.calls == 1
    assert store.upserts and store.upserts[0]["provider"] == "deep"  # 原 provider 保留
    assert store.upserts[0]["user_id"] == "u9"  # 归属保留（owner 守卫下必须同 owner）
    assert store.upserts[0]["title"] == "原标题"
