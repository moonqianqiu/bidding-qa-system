"""D1 search_knowledge：知识库检索工具（docs/02 D 域；包装 M1 Retriever + E6 CRAG-lite）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from bidqa.clients.llm import ChatClient
from bidqa.rag.crag import CRAG_THRESHOLD, crag_retrieve
from bidqa.rag.retrieve import Retriever
from bidqa.tools.base import BaseTool, empty_result, tool_result


class SearchKnowledgeArgs(BaseModel):
    query: str = Field(min_length=1, max_length=500, description="检索问题原文")
    top_k: int | None = Field(default=None, ge=1, le=20, description="返回条数上限（缺省用检索参数表 final_top_k）")


class SearchKnowledgeTool(BaseTool[SearchKnowledgeArgs]):
    name = "search_knowledge"
    description = "检索招投标知识库（法规/流程/FAQ）。返回带 [n] 编号的材料文本与来源条目，供引用标注。"
    args_model = SearchKnowledgeArgs

    def __init__(self, retriever: Retriever, chat: ChatClient | None = None,
                 crag_threshold: float = CRAG_THRESHOLD):
        self.retriever = retriever
        self.chat = chat
        self.crag_threshold = crag_threshold

    def run(self, args: SearchKnowledgeArgs) -> dict[str, Any]:
        top_k = args.top_k if args.top_k is not None else None
        result = self.retriever.retrieve(args.query, top_k=top_k) if top_k else self.retriever.retrieve(args.query)
        result = crag_retrieve(self.retriever, self.chat, args.query, result,
                               threshold=self.crag_threshold)
        if not result.hits:
            return empty_result()
        blocks = "\n\n".join(
            f"[{i + 1}] ({h.source_type}) {h.title}\n{h.text}"
            for i, h in enumerate(result.hits)
        )
        return tool_result(
            text=blocks + (f"\n\n（检索模式: {result.mode}；降级: {result.degraded}）" if result.degraded else ""),
            sources=[h.as_source() for h in result.hits],
        )
