"""聊天槽位可用性（docs/01 §5）：api 前置门与 worker 主路径共用的单一判据。

判定口径与 worker 主路径完全一致：`build_chat_for(provider)`——缺省/未知
provider 回落主槽位，仅当「该 provider 实际会用的客户端不存在」才算不可用。
此前 api 直接查 `build_chat()`（主槽位），导致只配命名档的部署被整体 5031
（2026-11-14 审计实锤的功能缺陷）。
"""

from __future__ import annotations

from bidqa.clients.chat_factory import build_chat_for


def chat_available(provider: str | None) -> bool:
    """该 provider 实际会用的聊天客户端是否存在（纯配置判定，零网络调用）。"""
    return build_chat_for(provider) is not None
