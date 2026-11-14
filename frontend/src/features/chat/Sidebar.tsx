"use client";

// 会话侧栏（A1/A2/A13/WP4 + 批量清理）：列表/搜索/多选条/重命名行——纯展示件。

import { useI18n } from "@/lib/i18n";
import type { AuthUser } from "@/lib/auth";
import type { Conversation } from "@/lib/types";
import UserBox from "@/features/chat/UserBox";

interface Props {
  authUser: AuthUser | null;
  canExport: boolean;
  onOpenSettings: () => void;
  onExport: () => void;
  onLogout: () => void;
  activeId: string | null;
  searchText: string;
  filteredConvos: Conversation[];
  healthLabel: string;
  selecting: boolean;
  selectedIds: Set<string>;
  renamingId: string | null;
  renameText: string;
  onNewChat: () => void;
  onToggleSelect: () => void;
  onSearch: (v: string) => void;
  onSelectAll: () => void;
  onDeleteSelected: () => void;
  onExitSelect: () => void;
  onOpen: (id: string) => void;
  onToggleSelectRow: (id: string) => void;
  onRenameStart: (c: Conversation) => void;
  onRenameText: (v: string) => void;
  onRenameCommit: () => void;
  onRenameCancel: () => void;
  onDeleteAsk: (c: Conversation) => void;
}

export default function Sidebar(p: Props) {
  const { t } = useI18n();
  return (
    <aside className="hidden w-60 shrink-0 flex-col border-r border-line/80 bg-panel/40 md:flex">
      <div className="p-3">
        <h1 className="text-sm font-semibold text-fg">{t.appName}</h1>
        <p className="mt-0.5 text-[10px] text-faint">{p.healthLabel}</p>
      </div>
      <div className="mx-3 flex gap-1.5">
        <button
          onClick={p.onNewChat}
          className="flex-1 rounded-lg border border-line py-1.5 text-xs text-dim hover:border-sky-800 hover:text-sky-300"
        >
          {t.newChatBtn}
        </button>
        {/* 批量清理入口（2026-11-08）：进入多选模式 */}
        <button
          onClick={p.onToggleSelect}
          title={t.multiSelect}
          className={`rounded-lg border px-2.5 py-1.5 text-xs ${
            p.selecting
              ? "border-sky-800 bg-sky-950/40 text-sky-300"
              : "border-line text-dim hover:border-sky-800 hover:text-sky-300"
          }`}
        >
          ☑
        </button>
      </div>
      {/* A13 会话搜索：标题+消息关键词本地过滤（docs/02 A13，M4 后演进服务端检索） */}
      <input
        value={p.searchText}
        onChange={(e) => p.onSearch(e.target.value)}
        placeholder={t.searchConvos}
        className="mx-3 mt-2 w-[calc(100%-1.5rem)] rounded-lg border border-line bg-panel px-2 py-1.5 text-xs text-fg outline-none placeholder:text-faint focus:border-sky-800"
      />
      {/* 多选模式操作条：计数 / 全选（限可见项）/ 删除所选 / 取消 */}
      {p.selecting && (
        <div className="mx-3 mt-2 flex items-center justify-between gap-1 rounded-lg border border-line bg-panel px-2 py-1.5 text-xs">
          <span className="shrink-0 text-dim">{t.selectedCount(p.selectedIds.size)}</span>
          <span className="flex shrink-0 gap-2">
            <button onClick={p.onSelectAll} className="text-sky-300 hover:text-sky-200">
              {t.selectAll}
            </button>
            <button
              onClick={p.onDeleteSelected}
              disabled={p.selectedIds.size === 0}
              className="text-red-300 hover:text-red-200 disabled:opacity-40"
            >
              {t.deleteSelected}
            </button>
            <button onClick={p.onExitSelect} className="text-faint hover:text-fg">
              {t.cancel}
            </button>
          </span>
        </div>
      )}
      <div className="mt-2 flex-1 space-y-0.5 overflow-y-auto px-2 pb-3">
        {p.filteredConvos.length === 0 && (
          <p className="px-2.5 py-2 text-xs text-faint">{t.noConvMatch}</p>
        )}
        {p.filteredConvos.map((c) => (
          <ConversationRow
            key={c.id}
            c={c} p={p}
          />
        ))}
      </div>
      {/* 设置入口 + 导出（游客隐藏）+ 当前用户（docs/01 §3.1 角色门控） */}
      <UserBox
        authUser={p.authUser}
        canExport={p.canExport}
        onOpenSettings={p.onOpenSettings}
        onExport={p.onExport}
        onLogout={p.onLogout}
      />
    </aside>
  );
}

function ConversationRow({ c, p }: { c: Conversation; p: Props }) {
  const { t } = useI18n();
  const renaming = p.renamingId === c.id;
  return (
    <div
      className={`group flex cursor-pointer items-center rounded-lg px-2.5 py-2 text-xs ${
        p.selecting
          ? p.selectedIds.has(c.id)
            ? "bg-sky-950/40 text-fg"
            : "text-dim hover:bg-panel"
          : c.id === p.activeId
            ? "bg-line/80 text-fg"
            : "text-dim hover:bg-panel"
      }`}
      onClick={() => (p.selecting ? p.onToggleSelectRow(c.id) : p.onOpen(c.id))}
    >
      {p.selecting && (
        <span
          className={`mr-1.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border text-[10px] leading-none ${
            p.selectedIds.has(c.id)
              ? "border-sky-600 bg-sky-800 text-white"
              : "border-line text-transparent"
          }`}
        >
          ✓
        </span>
      )}
      {renaming ? (
        <input
          autoFocus
          className="min-w-0 flex-1 rounded border border-line bg-panel px-1 py-0.5 text-xs text-fg outline-none"
          value={p.renameText}
          placeholder={t.renamePlaceholder}
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => p.onRenameText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") p.onRenameCommit();
            if (e.key === "Escape") p.onRenameCancel();
          }}
          onBlur={p.onRenameCommit}
        />
      ) : (
        <span className="min-w-0 flex-1 truncate">{c.title === "新会话" ? t.newChat : c.title}</span>
      )}
      {!p.selecting && !renaming && (
        <button
          className="ml-1 hidden text-faint hover:text-fg group-hover:block"
          title={t.rename}
          onClick={(e) => {
            e.stopPropagation();
            p.onRenameStart(c);
          }}
        >
          ✏️
        </button>
      )}
      {!p.selecting && (
        <button
          className="ml-1 hidden text-faint hover:text-red-400 group-hover:block"
          title={t.deleteConvTitle}
          onClick={(e) => {
            e.stopPropagation();
            p.onDeleteAsk(c); // 先确认再删（防手滑）
          }}
        >
          ✕
        </button>
      )}
    </div>
  );
}
