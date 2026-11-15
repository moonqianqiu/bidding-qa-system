"""D1 search_knowledge：知识库检索工具（docs/02 D 域；包装 M1 Retriever + E6 CRAG-lite）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from bidqa.clients.llm import ChatClient
from bidqa.rag.crag import CRAG_THRESHOLD, crag_retrieve
from bidqa.rag.retrieve import FINAL_TOP_K, Retriever
from bidqa.tools.base import BaseTool, empty_result, tool_result


class SearchKnowledgeArgs(BaseModel):
    # 多余参数一律拒绝（与 db/graph 工具同哲学，2026-11-15 审计 P2）
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500, description="检索问题原文")
    top_k: int | None = Field(default=None, ge=1, le=20, description="返回条数上限（缺省用检索参数表 final_top_k）")


class SearchKnowledgeTool(BaseTool[SearchKnowledgeArgs]):
    name = "search_knowledge"
    description = ("检索招投标知识库（法规/流程/FAQ 等文本知识）。返回带 [n] 编号的材料文本"
                   "与来源条目，供引用标注。业务库的数据统计/排名/明细问题不要用本工具"
                   "（走 search_database / search_graph）。")
    args_model = SearchKnowledgeArgs

    def __init__(self, retriever: Retriever, chat: ChatClient | None = None,
                 crag_threshold: float = CRAG_THRESHOLD):
        self.retriever = retriever
        self.chat = chat
        self.crag_threshold = crag_threshold

    def run(self, args: SearchKnowledgeArgs) -> dict[str, Any]:
        top_k = FINAL_TOP_K if args.top_k is None else args.top_k
        result = self.retriever.retrieve(args.query, top_k=top_k)
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
