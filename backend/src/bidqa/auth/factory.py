"""auth 层工厂（重构 C3，从 service.py 归位）：UsersStore 惰性构建 + 管理员种子。

PG 不可用 = 降级 None（鉴权中间件按降级语义放行/拒绝）；成功则首次构建时
种子管理员（users 空表才种，幂等），默认口令告警一次性提醒。
审计 P1-2：None 不再永久缓存（原 lru_cache 首次失败=进程内永远 None）——
重试节奏由 build_pg 的负缓存窗口统一收敛。
"""

from __future__ import annotations

from bidqa.config import get_settings

_users_store_cache: "UsersStore | None" = None


def build_users_store() -> "UsersStore | None":
    global _users_store_cache
    if _users_store_cache is not None:
        return _users_store_cache
    from bidqa.auth.store import UsersStore
    from bidqa.database.factory import build_pg

    pg = build_pg()
    if pg is None:
        return None
    s = get_settings()
    store = UsersStore(pg)
    if store.seed_if_empty(s.ADMIN_USERNAME, s.ADMIN_PASSWORD):
        import logging

        if s.ADMIN_PASSWORD == "admin123":
            logging.getLogger("bidqa").warning(
                "管理员 %s 以默认口令种子（admin/admin123），请尽快 `bidqa user reset` 改密",
                s.ADMIN_USERNAME)
    _users_store_cache = store
    try:
        if store.verify_password_ok(s.ADMIN_USERNAME, "admin123"):
            # 对账号真实哈希体检（P1-5 告警升级路径：源码默认空+启动拒绝的强制收口待用户拍板）
            logging.getLogger("bidqa").warning(
                "管理员账号 %s 仍在用公开默认口令 admin123——"
                "请 `uv run bidqa user reset admin --password <新口令>` 改密", s.ADMIN_USERNAME)
    except Exception:
        pass  # 体检是尽力而为的告警，不阻塞构建
    return store


def clear_users_store_cache() -> None:
    """测试隔离口（conftest 用）。"""
    global _users_store_cache
    _users_store_cache = None
