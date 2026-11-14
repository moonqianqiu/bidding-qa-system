"""身份模型（docs/01 §3.1 用户体系段）：三角色常量 + 请求身份。

从包根 auth_core.py 归位（重构 C1）：角色/身份是认证内核里被引用最广的纯定义，
单独成模块——「身份是什么」与「口令怎么存」（passwords）、「会话怎么管」（store）各归各文件。
"""

from __future__ import annotations

from dataclasses import dataclass

ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLE_GUEST = "guest"
MACHINE_USERNAME = "__machine__"  # 机器主 token 的虚拟身份名（无 users 行，user_id=None）


@dataclass
class Identity:
    """请求身份（中间件解析后存 request.state.identity，端点据此做数据隔离）。"""

    role: str  # admin / user / guest
    user_id: str | None  # guest 会话与机器主 token 为 None
    username: str
    token: str = ""  # 原始会话令牌（限流分桶用；机器主 token 不存）
