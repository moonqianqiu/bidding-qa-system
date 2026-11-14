"use client";

// 侧栏底部用户区：设置/导出（游客隐藏）/控制台（admin）/当前身份+角色徽章+登出。

import { useI18n } from "@/lib/i18n";
import type { AuthUser } from "@/lib/auth";

interface Props {
  authUser: AuthUser | null;
  canExport: boolean;
  onOpenSettings: () => void;
  onExport: () => void;
  onLogout: () => void;
}

export default function UserBox(p: Props) {
  const { t } = useI18n();
  return (
    <div className="border-t border-line/80 p-3">
      <button
        onClick={p.onOpenSettings}
        className="flex w-full items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs text-dim hover:bg-panel hover:text-fg"
      >
        ⚙ {t.settings}
      </button>
      {p.authUser?.role !== "guest" && (
        <button
          onClick={p.onExport}
          disabled={!p.canExport}
          className="flex w-full items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs text-dim hover:bg-panel hover:text-fg disabled:opacity-40"
        >
          ⬇ {t.exportChat}
        </button>
      )}
      {p.authUser?.role === "admin" && (
        <a
          href="/console"
          className="flex w-full items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs text-dim hover:bg-panel hover:text-fg"
        >
          ▦ {t.consoleTitle}
        </a>
      )}
      {p.authUser && (
        <div className="mt-1 flex items-center justify-between gap-1 rounded-lg px-2 py-1.5 text-xs">
          <span className="flex min-w-0 items-center gap-1.5">
            <span className="truncate text-dim">{p.authUser.username}</span>
            {p.authUser.role === "guest" && (
              <span className="shrink-0 rounded bg-amber-950/60 px-1.5 py-0.5 text-[10px] text-amber-300">
                {t.guestBadge}
              </span>
            )}
            {p.authUser.role === "admin" && (
              <span className="shrink-0 rounded bg-sky-950/60 px-1.5 py-0.5 text-[10px] text-sky-300">
                admin
              </span>
            )}
          </span>
          <button
            onClick={p.onLogout}
            title={t.logout}
            className="shrink-0 text-faint hover:text-fg"
          >
            ⎋
          </button>
        </div>
      )}
    </div>
  );
}
