"""GET /api/system/console（A15/B11）：三库规模聚合端点（KPI 仪表盘）。

业务聚合在 application/admin/console（诚实降级：任一库挂该节「不可用」）；
本模块只管 30s 缓存与 HTTP 形态（health 同款缓存语义）。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends

from bidqa.application.admin import console as console_app
from bidqa.config import Settings, get_settings

router = APIRouter()

_console_cache: tuple[float, dict] | None = None
_CONSOLE_TTL = 30.0


@router.get("/api/system/console")
def system_console(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """三库规模聚合：pg{bidding_procurement, conversations, feedback, users} +
    qdrant{vectors, collection} + neo4j{labels 六类, total}。任一库挂 → 该节
    {"status": "不可用"}，其余节照常（诚实降级，不整页报废）。"""
    global _console_cache
    now = time.monotonic()
    if _console_cache and now - _console_cache[0] < _CONSOLE_TTL:
        return _console_cache[1]
    data = console_app.collect_all(settings)
    _console_cache = (now, data)
    return data
