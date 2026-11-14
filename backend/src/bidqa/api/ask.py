"""POST /api/ask（docs/01 §3.1 B1；M1-8）：直连知识库问答（非流式，长期保留）。

流程：检索（E1-E5，含灾备）→ 生成（chat）→ 答案+来源条目+phase_times。
任一环节整体不可用 → 5031（body 注明哪个源），绝不挂死。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bidqa.api.ratelimit import rate_identity
from bidqa.clients.llm import ChatClient
from bidqa.config import get_settings
from bidqa.errors import EC_RATE_LIMITED, EC_SOURCE_DEGRADED, error_body
from bidqa.rag.crag import crag_retrieve
from bidqa.rag.retrieve import FINAL_TOP_K, RetrievalResult, Retriever
from bidqa.service import build_chat, build_retriever

router = APIRouter()

SYSTEM_PROMPT = (
    "你是招投标领域问答助手。只依据给定的【检索材料】作答；材料未覆盖的部分回答"
    "「知识库未包含」。答案中引用材料时标注 [n]（n 为材料序号）。"
)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=FINAL_TOP_K, ge=1, le=20)


def _build_messages(question: str, result: RetrievalResult) -> list[dict]:
    blocks = "\n\n".join(
        f"[{i + 1}] ({h.source_type}) {h.title}\n{h.text}"
        for i, h in enumerate(result.hits)
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"【检索材料】\n{blocks}\n\n【问题】{question}"},
    ]


def _json_5031(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content=error_body(
            EC_SOURCE_DEGRADED, "数据源降级/不可用", detail={"source": detail}
        ),
    )


@router.post("/api/ask")
def ask(payload: AskRequest, request: Request) -> dict[str, Any]:
    settings = get_settings()
    limiter, bucket = rate_identity(request, settings)  # B4；游客走独立更严配额桶
    allowed, retry_after = limiter.check(bucket)
    if not allowed:  # B4：问答类端点限流；被限流不计在线评测
        return JSONResponse(
            status_code=429,
            content=error_body(
                EC_RATE_LIMITED,
                "请求过于频繁，请稍后再试",
                detail={"retry_after": retry_after},
            ),
            headers={"Retry-After": str(retry_after)},
        )
    retriever: Retriever = build_retriever()
    chat: ChatClient | None = build_chat()
    from bidqa.api.system import note_request

    note_request("/api/ask", 200)  # H2 行为计数

    t0 = time.perf_counter()
    try:
        result = retriever.retrieve(payload.question, top_k=payload.top_k)
        result = crag_retrieve(
            retriever, chat, payload.question, result
        )  # E6 CRAG-lite
    except Exception as exc:  # 检索整体不可用（双模式全失败）
        return _json_5031(f"retrieval: {exc}")
    retrieve_ms = int((time.perf_counter() - t0) * 1000)

    if not result.hits:
        return {
            "answer": "知识库未包含相关内容。",
            "sources": [],
            "degraded": result.degraded,
            "mode": result.mode,
            "phase_times": {
                "retrieve_ms": retrieve_ms,
                "generate_ms": 0,
                "total_ms": retrieve_ms,
            },
        }

    if chat is None:
        return _json_5031("chat: 核心槽位未配置（CHAT_*）")

    t1 = time.perf_counter()
    try:
        answer = chat.chat(_build_messages(payload.question, result))
    except Exception as exc:
        return _json_5031(f"chat: {exc}")
    generate_ms = int((time.perf_counter() - t1) * 1000)

    return {
        "answer": answer,
        "sources": [h.as_source() for h in result.hits],
        "mode": result.mode,
        "degraded": result.degraded,
        "phase_times": {
            "retrieve_ms": retrieve_ms,
            "generate_ms": generate_ms,
            "total_ms": retrieve_ms + generate_ms,
        },
    }
