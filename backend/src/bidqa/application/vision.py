"""视觉用例（docs/01 §3.1/§3.3）：image_ref TTL 缓存 + 图片上下文块组装。

从 api/vision.py 与 chat_stream._image_context_block 归位（重构 C3）：
- 描述缓存是跨端点共享的进程内状态（vision 写入、chat worker 读取）——
  放 application 层后 chat 不再反向依赖 api.vision（test_layering R5 清零）；
- HTTP 状态码（413/400/503）留在 api/vision，本模块只管用例与状态。
"""

from __future__ import annotations

import threading
import time
import uuid

from bidqa.clients.chat_factory import build_vision  # noqa: F401  # patch 缝（测试替身注入点）

REF_TTL_SECONDS = 600.0  # §3.3：服务端缓存图片描述 TTL 10 分钟
REF_STORE_MAX = 64  # 有界防泄漏；满了先清过期再丢最旧（web_cache 同款策略）

VISION_SYSTEM_PROMPT = (
    "你是招投标文件图片解析助手。只描述图片中真实可见的内容（文字/表格/数字/布局），"
    "不推测、不编造；看不清的部分如实说明。")
VISION_USER_PROMPT = "请解析这张图片的内容，输出结构化的文字描述。"


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


class VisionUnavailable(Exception):
    """视觉解析不可用（API 层转 5031；str = body 里的 source 细节）。"""


def describe_image(data_url: str) -> str:
    """视觉槽位解析（多模态 list-content，同一 OpenAI-compatible 适配器）。

    解析失败/空描述 → VisionUnavailable（API 层 5031），绝不返回空描述充数。
    """
    client = build_vision()
    if client is None:
        raise VisionUnavailable("vision: 视觉槽位未配置（VISION_*）")
    try:
        description = str(client.chat([
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": VISION_USER_PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ], temperature=0.1)).strip()
    except Exception as exc:
        raise VisionUnavailable(f"vision: {type(exc).__name__}: {str(exc)[:120]}") from exc
    if not description:
        raise VisionUnavailable("vision: 上游返回空描述")
    return description
