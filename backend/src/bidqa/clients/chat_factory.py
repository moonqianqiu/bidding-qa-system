"""聊天槽位客户端工厂（docs/01 §5 三槽位；C8/A12 命名档路由）。

从 service.py 归位（重构 C1 pilot）：「按 provider 选客户端」是聊天运行时的窄依赖，
不该牵动全部 builder。主槽位 lru_cache 单例；命名档客户端按名缓存（进程内状态，
单进程约定 docs/01 §6）。/api/system/providers 只读 Settings 脱敏名册，不经本模块。
"""

from __future__ import annotations

from functools import lru_cache

from bidqa.clients.llm import ChatClient
from bidqa.config import get_settings


@lru_cache(maxsize=1)
def build_chat() -> ChatClient | None:
    """主槽位客户端（三件套缺失 = None，降级语义见 docs/01 §5）。"""
    s = get_settings()
    if not (s.CHAT_BASE_URL and s.CHAT_API_KEY and s.CHAT_MODEL):
        return None
    return ChatClient(s.CHAT_BASE_URL, s.CHAT_API_KEY, s.CHAT_MODEL,
                      max_tokens=s.CHAT_MAX_TOKENS)


_profile_chats: dict[str, ChatClient] = {}


def build_chat_for(provider: str | None) -> ChatClient | None:
    """C8/A12：按 provider 名走命名档；未指定/未配置回落主槽位。"""
    if not provider:
        return build_chat()
    profile = get_settings().chat_profiles().get(provider)
    if profile is None:
        return build_chat()
    client = _profile_chats.get(provider)
    if client is None:
        client = ChatClient(profile["base_url"], profile["api_key"], profile["model"])
        _profile_chats[provider] = client
    return client


def build_subject_batch_chat() -> ChatClient | None:
    """数据加工批量通道（SUBJECT_BATCH_*）：subject-batch 与图谱归一共用。

    与三个运行时槽位（CHAT/VISION/JUDGE）独立：离线批量任务专用，未配置=调用方
    诚实拒绝（返回 None，不回落主槽位）。单进程内按需构造，不做单例缓存。
    """
    s = get_settings()
    if not s.has_subject_batch:
        return None
    return ChatClient(s.SUBJECT_BATCH_BASE_URL, s.SUBJECT_BATCH_API_KEY,
                      s.SUBJECT_BATCH_MODEL)


@lru_cache(maxsize=1)
def build_vision() -> ChatClient | None:
    """视觉槽位客户端（未配置=降级 None，/api/vision 5031；同一 OpenAI-compatible 适配器）。"""
    s = get_settings()
    if not s.has_vision:
        return None
    return ChatClient(s.VISION_BASE_URL, s.VISION_API_KEY, s.VISION_MODEL)
