"""GET /api/system/providers（A12/C8）：主槽位命名档脱敏名册（前端「模型」下拉用）。

从 system.py 独立成模块（重构 C1 pilot）：它是纯配置只读视图——只读 Settings、
只回名字与模型名，绝不回传 key，也不接触运行时客户端（clients/chat_factory）。
admin-only 门禁由中间件按 /api/system 前缀统一执行。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from bidqa.config import get_settings

router = APIRouter()


@router.get("/api/system/providers")
def system_providers(settings: Any = Depends(get_settings)) -> dict[str, Any]:
    """A12/C8 档位列表（前端下拉用；只给名字与模型，绝不回传 key）。
    settings 经 DI 注入（工厂 override 可测试，同 vision 2026-11-14 修复口径）。"""
    profiles = [{"name": name, "model": p.get("model", "")}
                for name, p in settings.chat_profiles().items()
                if p.get("base_url") and p.get("api_key") and p.get("model")]
    return {"default_model": settings.CHAT_MODEL, "profiles": profiles}
