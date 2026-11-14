"""降级直答用例（docs/01 §3.6 C6）：Agent 基建失败时的无工具 RAG 直连。

从 chat_stream._direct_rag 归位（重构 C3）：提示词收口 agent/prompts（消除与
/api/ask 的措辞漂移）；段名 retrieve_ms/degrade_ms 与主路径两套不合并（§3.4）。
调用方（worker）负责再败时的 error 事件转换。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from bidqa.agent.loop import LoopResult
from bidqa.agent.prompts import RAG_DIRECT_SYSTEM, RAG_DIRECT_SYSTEM_EN_SUFFIX
from bidqa.application.vision import image_context_block
from bidqa.clients.chat_factory import build_chat
from bidqa.clients.siliconflow import DataSourceError
from bidqa.rag.factory import build_retriever

EventFn = Callable[[str, dict[str, Any]], None]


def direct_rag_answer(question: str, lang: str, image_refs: list[str],
                      emit: EventFn, reason: str) -> LoopResult:
    """降级直答：检索 → 有料生成（真流式）/ 无料诚实兜底；不编造、不挂死。"""
    emit("status", {"stage": "降级直答"})
    t0 = time.perf_counter()
    retriever = build_retriever()
    chat = build_chat()
    if retriever is None or chat is None:
        raise DataSourceError("chat", "降级路径不可用")
    result = retriever.retrieve(question)
    retrieve_ms = int((time.perf_counter() - t0) * 1000)
    blocks = "\n\n".join(f"[{i + 1}] ({h.source_type}) {h.title}\n{h.text}"
                         for i, h in enumerate(result.hits))
    image_block = image_context_block(image_refs)  # 降级路径同样带图片上下文
    answer = ("The knowledge base does not cover this content." if lang == "en"
              else "知识库未包含相关内容。")
    if result.hits:
        user_content = f"【检索材料】\n{blocks}\n\n【问题】{question}"
        if image_block:
            user_content = f"{user_content}\n\n{image_block}"
        system_prompt = RAG_DIRECT_SYSTEM
        if lang == "en":
            system_prompt += RAG_DIRECT_SYSTEM_EN_SUFFIX
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        parts: list[str] = []
        try:
            for chunk in chat.chat_stream(messages):
                parts.append(chunk)
                emit("token", {"text": chunk})  # 降级路径同样真流式
            answer = "".join(parts) or answer
        except Exception:
            answer = chat.chat(messages)
            for i in range(0, len(answer), 80):
                emit("token", {"text": answer[i:i + 80]})
    else:
        emit("token", {"text": answer})
    out = LoopResult(answer=answer, rounds=0,
                     sources=[h.as_source() for h in result.hits])
    out.phase_times = {"retrieve_ms": retrieve_ms,
                       "degrade_ms": int((time.perf_counter() - t0) * 1000) - retrieve_ms}
    out.degraded.append(f"agent_fallback: {reason[:120]}")
    return out
