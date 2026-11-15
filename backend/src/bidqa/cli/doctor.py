"""doctor 子命令：配置体检 + 能力摘要 + 语料覆盖度 + 用户体系态。

取不到的数值记「不可用」绝不记 0（跨层硬约束）；默认口令告警一次性提醒。
"""

from __future__ import annotations

from typing import Any


def cmd_doctor(_args, settings: Any) -> int:
    missing = settings.missing_core()
    if missing:
        print("核心配置缺失: " + ", ".join(missing))
        print("→ 复制 backend/.env.example 为 backend/.env 并填写三槽位")
        return 1
    caps = {
        "thinking": settings.has_thinking, "vision": settings.has_vision,
        "judge": settings.has_judge, "embedding": settings.has_embedding,
        "rerank": settings.has_rerank, "neo4j": settings.has_neo4j,
        "postgres": settings.has_postgres, "web_search": settings.has_web_search,
        "pdf_api": settings.has_pdf_api, "subject_batch": settings.has_subject_batch,
    }
    print("核心配置 OK")
    print("鉴权模式:", "token" if settings.auth_enabled else "dev（未配置 API_AUTH_TOKEN，放行）")
    for name, ok in caps.items():
        print(f"  {name:<10} {'就绪' if ok else '降级（未配置）'}")
    # 语料覆盖度：库里到底有哪几类 source_type（失败记「不可用」，绝不记 0）
    try:
        from bidqa.database.factory import build_repo
        dist = build_repo().source_type_counts()
        print(f"  source_type 分布: {dist or '（无数据）'}")
    except Exception:
        print("  source_type 分布: 不可用")
    # 用户体系态（docs/01 §3.1）：开关/用户数/游客开关/默认口令警告
    if not settings.USERS_ENABLED:
        print("用户体系: 关（USERS_ENABLED=false，单机 token 模式）")
        return 0
    try:
        from bidqa.auth.factory import build_users_store
        store = build_users_store()
    except Exception:
        store = None
    if store is None:
        print("  用户体系  降级（PG 不可用，登录不可用）")
        return 0
    try:
        n_users = store.count_users()
        guest = "开" if store.guest_enabled() else "关"
        print(f"  用户体系  就绪（users={n_users}，游客开关={guest}，会话 TTL={settings.SESSION_TTL_HOURS}h）")
        names = {u["username"] for u in store.list_users()}
        if settings.ADMIN_USERNAME in names and store.verify_password_ok(
                settings.ADMIN_USERNAME, "admin123"):
            # 对账号真实哈希体检（.env 配置值≠账号实际口令，2026-11-15 P1-5 收口）
            print("  ⚠ 管理员账号仍在用公开默认口令 admin123（任何非纯本机场景必须改）："
                  "`uv run bidqa user reset admin --password <新口令>`")
    except Exception as exc:
        print(f"  用户体系  异常（{type(exc).__name__}: {exc}）")
    return 0
