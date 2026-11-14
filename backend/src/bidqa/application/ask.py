"""直连知识库问答用例（docs/01 §3.1 B1 / §3.6）：检索(+CRAG) → 生成 → 契约体。

从 api/ask.py 下沉（重构 C4）：本模块不 import FastAPI——源不可用经
AskUnavailable 表达（API 层转 5031，body 注明哪个源），成功返回端点契约 dict。
语义保持：无命中的诚实回答优先于 chat 未配置的 5031（先检索后判能力）。
"""

from __future__ import annotations

import time

from bidqa.agent.prompts import RAG_DIRECT_SYSTEM
from bidqa.clients.chat_factory import build_chat
from bidqa.rag.crag import crag_retrieve
from bidqa.rag.factory import build_retriever


class AskUnavailable(Exception):
    """检索/生成整体不可用（API 层转 5031；str = body 里的 source 细节）。"""


def _build_messages(question: str, result) -> list[dict]:
    blocks = "\n\n".join(
        f"[{i + 1}] ({h.source_type}) {h.title}\n{h.text}"
        for i, h in enumerate(result.hits)
    )
    return [
        {"role": "system", "content": RAG_DIRECT_SYSTEM},
        {"role": "user", "content": f"【检索材料】\n{blocks}\n\n【问题】{question}"},
    ]


def ask_direct(question: str, top_k: int) -> dict:
    """直连问答：答案 + 来源条目 + phase_times（段名主路径口径）。"""
    retriever = build_retriever()
    chat = build_chat()
    t0 = time.perf_counter()
    try:
        result = retriever.retrieve(question, top_k=top_k)
        result = crag_retrieve(retriever, chat, question, result)  # E6 CRAG-lite
    except Exception as exc:  # 检索整体不可用（双模式全失败）
        raise AskUnavailable(f"retrieval: {exc}") from exc
    retrieve_ms = int((time.perf_counter() - t0) * 1000)

    if not result.hits:
        return {
            "answer": "知识库未包含相关内容。",
            "sources": [],
            "degraded": result.degraded,
            "mode": result.mode,
            "phase_times": {"retrieve_ms": retrieve_ms, "generate_ms": 0,
                            "total_ms": retrieve_ms},
        }

    if chat is None:
        raise AskUnavailable("chat: 核心槽位未配置（CHAT_*）")

    t1 = time.perf_counter()
    try:
        answer = chat.chat(_build_messages(question, result))
    except Exception as exc:
        raise AskUnavailable(f"chat: {exc}") from exc
    generate_ms = int((time.perf_counter() - t1) * 1000)

    return {
        "answer": answer,
        "sources": [h.as_source() for h in result.hits],
        "mode": result.mode,
        "degraded": result.degraded,
        "phase_times": {"retrieve_ms": retrieve_ms, "generate_ms": generate_ms,
                        "total_ms": retrieve_ms + generate_ms},
    }
