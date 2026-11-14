"""Tavily 联网搜索客户端（docs/02 D4）；Exa 同构客户端见 exa.py（REST 直调，经 WEB_SEARCH_PROVIDERS 名册启用）。"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

import httpx

from bidqa.clients.siliconflow import _post_with_retry

TAVILY_URL = "https://api.tavily.com/search"


class WebCache:
    """D4/D5 共享缓存：键 (kind, query, provider, depth)；TTL 600s/512 条；只缓存成功结果。"""

    TTL = 600.0
    SIZE = 512

    def __init__(self) -> None:
        self._data: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()

    def get(self, key: tuple) -> Any | None:
        if key in self._data:
            ts, value = self._data[key]
            if time.monotonic() - ts <= self.TTL:
                self._data.move_to_end(key)
                return value
            self._data.pop(key)
        return None

    def put(self, key: tuple, value: Any) -> None:
        self._data[key] = (time.monotonic(), value)
        self._data.move_to_end(key)
        while len(self._data) > self.SIZE:
            self._data.popitem(last=False)


_shared_cache = WebCache()


def web_cache() -> WebCache:
    return _shared_cache


class TavilyClient:
    def __init__(self, api_key: str, timeout: float = 30.0):
        self.api_key = api_key
        self._client = httpx.Client(timeout=timeout, trust_env=False)

    def search(self, query: str, depth: str = "basic", max_results: int = 5) -> list[dict]:
        data = _post_with_retry(
            self._client, TAVILY_URL,
            {"api_key": self.api_key, "query": query, "search_depth": depth,
             "max_results": max_results},
            source="web_search",
        )
        return [
            {"title": r.get("title", ""), "url": r.get("url", ""),
             "content": (r.get("content") or "")[:500]}
            for r in data.get("results", [])
        ]

    def ping(self) -> bool:
        return bool(self.api_key)

    def close(self) -> None:
        self._client.close()
