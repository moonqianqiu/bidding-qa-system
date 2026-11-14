"""Qwen 视觉通道（docs/01 §3.1）：多模态解析调用与不可用异常。

从 application/vision.py 归位（v2.3 收拢进 application/image 包）。
build_vision 是测试 patch 缝（noqa: F401，打桩注入点）。
"""

from __future__ import annotations

from bidqa.application.image import prompts
from bidqa.clients.chat_factory import build_vision  # noqa: F401  # patch 缝（测试替身注入点）


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
            {"role": "system", "content": prompts.VISION_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": prompts.VISION_USER_PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ], temperature=0.1)).strip()
    except Exception as exc:
        raise VisionUnavailable(f"vision: {type(exc).__name__}: {str(exc)[:120]}") from exc
    if not description:
        raise VisionUnavailable("vision: 上游返回空描述")
    return description
