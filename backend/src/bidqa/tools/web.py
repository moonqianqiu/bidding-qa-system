"""D4/D5 search_web：联网检索工具——Tavily/Exa 多供应商（供应商由配置层决定）。

供应商名册 = WEB_SEARCH_PROVIDERS（config.web_search_providers()，留空=联网关闭），
客户端 dict 传入（provider 名→search 客户端，配置序即合并序）；缓存键
("web", query, providers, depth) 沿用 D4 provider 槽契约（多供应商逗号连接）。
未配置时本工具不注册（C2 挂载跳过）。只缓存成功结果；缓存返回深拷贝。
合并语义：逐供应商检索 → URL 去重 keep-first；某供应商失败用成功者继续但**不缓存**
（部分结果）；全败 → empty_result 诚实文案。
可选重排（参考 §15）：reranker 传入时按检索 query 对合并结果做相关性重排——分数 min-max
归一 [0,1] 写回 sources.score（只标注不重排序，text 块 [1..n] 序号保持对齐）；
重排失败 → 引擎原序 + score None（诚实降级）。归一后缓存：同 query 确定性，命中免重跑。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Protocol

from pydantic import BaseModel, Field

from bidqa.clients.siliconflow import DataSourceError, RerankClient
from bidqa.clients.tavily import WebCache
from bidqa.tools.base import BaseTool, empty_result, tool_result


class SearchWebArgs(BaseModel):
    query: str = Field(min_length=2, max_length=400, description="联网检索查询词")
    max_results: int | None = Field(default=None, ge=1, le=10)


class SearchClient(Protocol):
    """TavilyClient/ExaClient 的共同形状（鸭子类型；测试 Fake 同此签名）。"""

    def search(self, query: str, depth: str = "basic", max_results: int = 5) -> list[dict]: ...


class SearchWebTool(BaseTool):
    name = "search_web"
    args_model = SearchWebArgs

    def __init__(self, clients: dict[str, SearchClient], cache: WebCache | None = None,
                 depth: str = "basic", reranker: RerankClient | None = None):
        self.clients = clients  # provider 名 → 检索客户端
        self.cache = cache or WebCache()
        self.depth = depth  # 深度 fixed basic（D4；Exa 端映射 auto）
        self.reranker = reranker  # RERANK_* 已配置时才传入；缺失=不重排
        # 描述动态列出供应商（描述质量直接影响工具选择率）
        self.description = (
            f"联网搜索（{'/'.join(clients)}）：招投标领域之外的时效性信息、公开公告原文。"
            "返回标题/链接/摘要。")

    def run(self, args: SearchWebArgs) -> dict[str, Any]:
        max_results = args.max_results or 5
        key = ("web", args.query, ",".join(self.clients), self.depth)  # D4 缓存键契约
        if (cached := self.cache.get(key)) is not None:
            return deepcopy(cached)
        results: list[dict] = []
        partial = False
        for client in self.clients.values():
            try:
                results.extend(client.search(args.query, depth=self.depth,
                                             max_results=max_results))
            except DataSourceError:
                partial = True  # 单供应商失败：用成功者继续，但不缓存部分结果
        merged: list[dict] = []
        seen: set[str] = set()
        for r in results:  # URL 去重 keep-first（配置序）
            url = r.get("url") or ""
            if url and url not in seen:
                seen.add(url)
                merged.append(r)
        if not merged:
            return empty_result("联网搜索暂不可用，可稍后重试或改用知识库检索。")
        text = "\n".join(f"[{i + 1}] {r['title']}\n    {r['url']}\n    {r['content'][:200]}"
                         for i, r in enumerate(merged))
        sources = [{"id": f"web:{r['url']}", "source_type": "web", "title": r["title"],
                    "text": r["content"], "url": r["url"], "score": None}
                   for r in merged]
        self._rerank(args.query, sources)
        payload = tool_result(text=text, sources=sources)
        if not partial:
            self.cache.put(key, payload)  # 只缓存成功结果（部分失败不缓存）
        return deepcopy(payload)  # 缓存返回深拷贝（跨层硬约束）

    def _rerank(self, query: str, sources: list[dict[str, Any]]) -> None:
        """相关性重排（参考 §15）：分数归一写回，失败原样跳过；不改变来源顺序。"""
        if self.reranker is None or not sources:
            return
        try:
            ranked = self.reranker.rerank(
                query, [str(s.get("text") or "") for s in sources], top_n=len(sources))
        except Exception:
            return  # 重排失败 = 引擎原序 + score None（可选增强，不值得为它让搜索失败）
        raw = {int(i): float(score) for i, score in ranked if 0 <= int(i) < len(sources)}
        if not raw:
            return
        lo, hi = min(raw.values()), max(raw.values())
        span = hi - lo
        if span <= 0:
            return  # 全部同分无法归一 → 保持 None（绝不记 0 假装测过）
        for idx, score in raw.items():
            sources[idx]["score"] = round((score - lo) / span, 4)
