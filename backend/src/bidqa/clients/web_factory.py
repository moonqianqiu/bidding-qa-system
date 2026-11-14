"""联网检索客户端工厂（docs/01 §5 D4/D5）：按 WEB_SEARCH_PROVIDERS 名册构建。

从 service.py 归位（重构 C3）：名册∩key 双门槛，空 dict = 无联网（工具不注册，
前端不显示联网按钮）；配置序 = 检索合并序。
"""

from __future__ import annotations

from functools import lru_cache

from bidqa.clients.exa import ExaClient
from bidqa.clients.tavily import TavilyClient
from bidqa.config import get_settings


@lru_cache(maxsize=1)
def build_web_clients() -> dict[str, TavilyClient | ExaClient]:
    """联网客户端按名册构建（名册∩key；空 dict=无联网）。"""
    s = get_settings()
    clients: dict[str, TavilyClient | ExaClient] = {}
    for provider in s.web_search_providers():
        clients[provider] = (TavilyClient(s.TAVILY_API_KEY) if provider == "tavily"
                             else ExaClient(s.EXA_API_KEY))
    return clients
