"""G2 判分槽位：异源指纹校验（docs/01 §3.2/§5；执行前强制）。

指纹 = (端点, 凭据, 模型)。判分与主槽位指纹完全相同 → 拒绝跑批（5032）；
仅端点相同 → 告警放行。判分客户端与 ChatClient 同一 OpenAI-compatible 适配器。
"""

from __future__ import annotations

import logging
from typing import Any

from bidqa.clients.llm import ChatClient
from bidqa.errors import EC_JUDGE_FINGERPRINT

logger = logging.getLogger("bidqa.eval")


class JudgeFingerprintError(RuntimeError):
    error_code = EC_JUDGE_FINGERPRINT


def _fingerprint(base_url: str, api_key: str, model: str) -> tuple[str, str, str]:
    key = api_key
    masked = key[:6] + "…" + key[-4:] if len(key) > 12 else key
    return base_url.rstrip("/"), masked, model


def chat_fingerprint(settings: Any) -> tuple[str, str, str]:
    return _fingerprint(settings.CHAT_BASE_URL, settings.CHAT_API_KEY, settings.CHAT_MODEL)


def judge_fingerprint(settings: Any) -> tuple[str, str, str]:
    return _fingerprint(settings.JUDGE_BASE_URL, settings.JUDGE_API_KEY, settings.JUDGE_MODEL)


def embedding_fingerprint(settings: Any) -> tuple[str, str, str]:
    """WP6 检索评测快照 env 用（密钥同款脱敏，不回显明文）；未配置时由调用方判 has_embedding。"""
    return _fingerprint(settings.EMBEDDING_BASE_URL, settings.EMBEDDING_API_KEY,
                        settings.EMBEDDING_MODEL)


def validate_heterogeneous(settings: Any) -> tuple[str, str, str]:
    """执行前强制校验（docs/02 G2）。完全相同 → 5032 拒跑；仅同端点 → 告警。"""
    if not settings.has_judge:
        raise JudgeFingerprintError("判分槽位未配置（JUDGE_*），评测不可用")
    chat_fp = chat_fingerprint(settings)
    judge_fp = judge_fingerprint(settings)
    if chat_fp == judge_fp:
        raise JudgeFingerprintError(
            f"判分与主槽位指纹完全相同，拒绝跑批（防自评作弊，docs/01 §3.2）: {judge_fp}")
    if chat_fp[0] == judge_fp[0]:
        logger.warning("判分与主槽位端点相同（仅告警放行）: %s", judge_fp[0])
    return judge_fp


def build_judge(settings: Any) -> ChatClient:
    """判分客户端（与主模型同一适配器，C8 语义）。"""
    return ChatClient(settings.JUDGE_BASE_URL, settings.JUDGE_API_KEY, settings.JUDGE_MODEL)
