"""装配兼容垫片（重构 C3 过渡层）——原组合根已按职责归位到各窄工厂：

- 聊天/视觉槽位 → clients/chat_factory（build_chat/build_chat_for/build_vision）
- 联网客户端   → clients/web_factory（build_web_clients）
- 数据库/存储  → database/factory（build_pg/build_bp_repo/build_conversation_store/
                 build_feedback_store/build_neo/build_repo）
- 检索侧       → rag/factory（build_embedding/build_reranker/build_retriever）
- 用户体系     → auth/factory（build_users_store）
- 高层装配     → C5 随 cli 拆包在 container.py 定型

本模块只剩再导出（同一函数对象，lru_cache 语义不变），供尚未迁移的调用方
（api/ask、api/health、api/vision、api/middleware、cli）平滑过渡；
C4/C5 迁完调用方后整体删除（grep bidqa.service 清零是最终验收项）。
测试以 monkeypatch 打 `bidqa.service.X` 时依然生效——延迟导入方在调用时取属性。
"""

from __future__ import annotations

from bidqa.auth.factory import build_users_store  # noqa: F401
from bidqa.clients.chat_factory import build_chat, build_chat_for, build_vision  # noqa: F401
from bidqa.clients.web_factory import build_web_clients  # noqa: F401
from bidqa.database.factory import (  # noqa: F401
    build_bp_repo, build_conversation_store, build_feedback_store, build_neo,
    build_pg, build_repo,
)
from bidqa.rag.factory import build_embedding, build_reranker, build_retriever  # noqa: F401
