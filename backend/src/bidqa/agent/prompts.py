"""提示词集中地（重构 C3 收口）：跨端点共用的提示词单处定义，消除措辞漂移。

漂移史：api/ask.py 的直答 system prompt 与 chat_stream._direct_rag 内嵌版各自演化
（2026-11-14 核实措辞已不同步）。收口原则：同一语义的提示词只允许一份定义；
单文件单用途的提示词（loop 的 system 组装/澄清兜底/压缩指令）留在各自模块，
不为集中而集中。以 ask 版措辞为准（更精确：说明 [n] 为材料序号）。
v2.2：直答消息拼装也收口于此——/api/ask、降级直答、离线评测（ragas）三处共用，
消掉 ragas 的第 3 份漂移副本。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# 检索直答（/api/ask 与降级路径共用；【检索材料】/【问题】段结构是两者契约）
RAG_DIRECT_SYSTEM = (
    "你是招投标领域问答助手。只依据给定的【检索材料】作答；材料未覆盖的部分回答"
    "「知识库未包含」。答案中引用材料时标注 [n]（n 为材料序号）。"
)
# 英文回答语言节（降级路径 lang=en 时追加；来源标题/引文保持原文）
RAG_DIRECT_SYSTEM_EN_SUFFIX = " 回答正文必须使用英文（English）；引用标注 [n] 保留。"


def rag_direct_system(lang: str = "zh") -> str:
    """检索直答 system prompt（唯一措辞来源；lang=en 追加英文语言节）。"""
    system = RAG_DIRECT_SYSTEM
    if lang == "en":
        system += RAG_DIRECT_SYSTEM_EN_SUFFIX
    return system


def rag_direct_blocks(hits: Iterable[Any]) -> str:
    """编号材料块（[n] (source_type) title\\ntext）；判分侧（ragas）单独取用。"""
    return "\n\n".join(
        f"[{i + 1}] ({h.source_type}) {h.title}\n{h.text}"
        for i, h in enumerate(hits))


def build_rag_direct_messages(hits: Iterable[Any], question: str, *, lang: str = "zh",
                              extra_user_suffix: str = "") -> list[dict]:
    """检索直答消息唯一拼装：[{system}, {user}]；hits 鸭子类型（.source_type/.title/.text）。

    extra_user_suffix：追加在 user 消息末尾（降级路径的图片上下文块）。
    """
    blocks = rag_direct_blocks(hits)
    user = f"【检索材料】\n{blocks}\n\n【问题】{question}"
    if extra_user_suffix:
        user = f"{user}\n\n{extra_user_suffix}"
    return [
        {"role": "system", "content": rag_direct_system(lang)},
        {"role": "user", "content": user},
    ]
