"""user 子命令（docs/01 §3.1）：add/reset/disable/enable/list。

首次调用即触发 build_users_store（schema 幂等建表 + 空表种子管理员）；
--password 缺省走 getpass 交互（避免口令进 shell 历史）。
"""

from __future__ import annotations

import getpass
import sys
from typing import Any


def cmd_user(args, _settings: Any) -> int:
    # 延迟导入：保持「每次调用取当前工厂属性」的 patch 语义（测试替身注入点）
    from bidqa.auth.factory import build_users_store

    store = build_users_store()
    if store is None:
        print("PostgreSQL 未配置/不可用，用户体系无法操作", file=sys.stderr)
        return 2
    action = args.user_action
    if action == "add":
        password = args.password or getpass.getpass(f"为 {args.name} 设置口令: ")
        uid = store.create_user(args.name.strip(), password, args.role)
        if uid is None:
            print(f"用户已存在：{args.name}", file=sys.stderr)
            return 1
        print(f"已创建 {args.role}: {args.name.strip()}（{uid}）")
        return 0
    if action == "reset":
        password = args.password or getpass.getpass(f"为 {args.name} 设置新口令: ")
        revoked = store.reset_password(args.name.strip(), password)
        if revoked is None:
            print(f"用户不存在：{args.name}", file=sys.stderr)
            return 1
        extra = f"（吊销会话 {revoked} 个）" if revoked else ""
        print(f"已重置口令：{args.name.strip()}{extra}")
        return 0
    if action in ("disable", "enable"):
        if not store.set_disabled(args.name.strip(), action == "disable"):
            print(f"用户不存在：{args.name}", file=sys.stderr)
            return 1
        print(f"已{'停用' if action == 'disable' else '启用'}：{args.name.strip()}")
        return 0
    if action == "list":
        rows = store.list_users()
        if not rows:
            print("（无用户）")
            return 0
        for r in rows:
            print(f"{r['username']:<24} {r['role']:<8} "
                  f"{'停用' if r['disabled'] else '启用':<4} {r['created_at']}")
        return 0
    return 2
