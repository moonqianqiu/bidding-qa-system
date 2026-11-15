"""高层装配组合根（重构 C5）：只装「整栈」对象，不做单个 builder 的大杂烩。

单个对象的构造已归各窄工厂（clients/database/rag/auth 的 factory.py）；本模块
只承载「需要一次拿到多个系统」的入口级装配（CLI 评测/诊断）。API 路径不经过
这里（走窄 factory），业务模块不得反向依赖本文件（test_layering R1 钉死）。
"""

from __future__ import annotations

from bidqa.agent.assembly import build_registry
from bidqa.clients.chat_factory import build_chat, build_chat_for
from bidqa.database.factory import build_bp_repo, build_neo
from bidqa.rag.factory import build_retriever, build_reranker
from bidqa.tools.runner import ToolRunner


def build_agent_stack(provider: str | None = None) -> tuple:
    """完整问答栈（评测/诊断入口用）：(chat, registry, runner)。

    chat 未配置 → (None, registry, runner)；registry 按可用组件装配（与线上
    worker 同一口径，唯一实现 agent/assembly.build_registry）。provider 指定
    命名档时走 C8/A12 路由。
    """
    chat = build_chat_for(provider) if provider else build_chat()
    registry = build_registry(build_retriever(), chat, build_bp_repo(), build_neo(),
                              {}, build_reranker())
    return chat, registry, ToolRunner()
