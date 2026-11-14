"""auth 层工厂（重构 C3，从 service.py 归位）：UsersStore 惰性构建 + 管理员种子。

PG 不可用 = 降级 None（鉴权中间件按降级语义放行/拒绝）；成功则首次构建时
种子管理员（users 空表才种，幂等），默认口令告警一次性提醒。
"""

from __future__ import annotations

from functools import lru_cache

from bidqa.config import get_settings


@lru_cache(maxsize=1)
def build_users_store() -> "UsersStore | None":
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
    return store
