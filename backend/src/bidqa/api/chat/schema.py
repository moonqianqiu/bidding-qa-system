"""POST /api/chat/stream 请求模型（docs/01 §3.3）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChatStreamRequest(BaseModel):
    conversation_id: str | None = None
    message: str = Field(min_length=1, max_length=4000)
    web_enabled: bool = False
    thinking_enabled: bool = False
    image_refs: list[str] = Field(default_factory=list, max_length=4)
    provider: str | None = Field(default=None, max_length=64)
    client_msg_id: str | None = Field(default=None, max_length=64)
    lang: Literal["zh", "en"] = "zh"  # 界面语言 → 回答语言（P1；旧 payload 缺省 zh 不受影响）
