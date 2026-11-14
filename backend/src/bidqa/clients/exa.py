"""Exa 联网搜索客户端（docs/02 D5：并入 search_web 第二供应商；REST 直调，非 MCP SDK）。

偏差说明（2026-11-08 实施定盘）：原 D5 spec 写「Exa 经官方 MCP SDK」，实施改 REST API
直调——免 MCP 重依赖，与「三槽位走通用适配器」的哲学一致。输出与 TavilyClient 同构
（[{title,url,content}]），共用 WebCache（缓存键 provider 槽区分）。
"""

from __future__ import annotations

import httpx

from bidqa.clients.siliconflow import _post_with_retry

EXA_URL = "https://api.exa.ai/search"

# depth 语义映射：D4 的 basic ↔ Exa 的 auto（keyword/neural/auto 三档取折中）
_DEPTH_TO_EXA = {"basic": "auto", "advanced": "neural"}


class ExaClient:
    def __init__(self, api_key: str, timeout: float = 30.0):
        self.api_key = api_key
        # Exa 鉴权走 x-api-key 头（非 Tavily 的 body 字段），挂在 Client 默认头上
        self._client = httpx.Client(timeout=timeout, trust_env=False,
                                    headers={"x-api-key": api_key})

    def search(self, query: str, depth: str = "basic", max_results: int = 5) -> list[dict]:
        data = _post_with_retry(
            self._client, EXA_URL,
            {"query": query, "numResults": max_results,
             "type": _DEPTH_TO_EXA.get(depth, "auto"), "contents": {"text": True}},
            source="web_search",
        )
        return [
            {"title": r.get("title", ""), "url": r.get("url", ""),
             "content": (r.get("text") or "")[:500]}
            for r in data.get("results", [])
        ]

    def ping(self) -> bool:
        return bool(self.api_key)

    def close(self) -> None:
        self._client.close()
