"""image_ref 存储（docs/01 §3.1/§3.3）：image_ref → 描述的进程内 TTL 缓存。

从 api/vision.py 与 chat_stream._image_context_block 归位（重构 C3），
v2.3 收拢进 application/image 包。描述缓存是跨端点共享的进程内状态
（vision 写入、chat worker 读取）——放 application 层后 chat 不再反向
依赖 api.vision（test_layering R5 清零）；HTTP 状态码留在 api/vision。
"""

from __future__ import annotations

import threading
import time
import uuid

REF_TTL_SECONDS = 600.0  # §3.3：服务端缓存图片描述 TTL 10 分钟
REF_STORE_MAX = 64  # 有界防泄漏；满了先清过期再丢最旧（web_cache 同款策略）


class RefStore:
    """image_ref → 描述的进程内 TTL 缓存（单进程约定；重启即清，前端重传即可）。"""

    def __init__(self, ttl: float = REF_TTL_SECONDS, max_entries: int = REF_STORE_MAX):
        self.ttl = ttl
        self.max_entries = max_entries
        self._items: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def put(self, description: str) -> str:
        now = time.monotonic()
        with self._lock:
            if len(self._items) >= self.max_entries:
                alive = {k: v for k, v in self._items.items() if now - v[1] < self.ttl}
                if len(alive) >= self.max_entries:  # 全活着：丢最旧（web_cache 同款策略）
                    oldest = sorted(alive, key=lambda k: alive[k][1])
                    for k in oldest[:len(alive) - self.max_entries + 1]:
                        alive.pop(k, None)
                self._items = alive
            ref = f"img_{uuid.uuid4().hex[:12]}"
            self._items[ref] = (description, now)
            return ref

    def get(self, ref: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            item = self._items.get(ref)
            if item is None:
                return None
            description, ts = item
            if now - ts >= self.ttl:
                self._items.pop(ref, None)
                return None
            return description


_store = RefStore()


def get_ref_store() -> RefStore:
    return _store


def image_context_block(refs: list[str]) -> str:
    """image_refs → 【图片内容】上下文块（§3.3：描述由 /api/vision 预解析缓存）。

    过期/未知 ref 静默跳过——图片上下文是尽力而为的补充，TTL 过了就没了，
    不值得为它让整轮问答失败。只进模型、不落库（会话存原始问题）。
    """
    if not refs:
        return ""
    descs = [d for d in (get_ref_store().get(ref) for ref in refs) if d]
    if not descs:
        return ""
    return "\n".join(f"【图片内容 {i}】{d}" for i, d in enumerate(descs, 1))
