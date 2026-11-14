"use client";

// 问答主页（docs/02 A1-A9）——薄壳（C7 拆分后只剩状态声明与布局组装）：
// 会话状态 useChatSessions / 问答流 useChatStream / 能力探测 useCapabilities /
// 主题 useTheme；侧栏/消息面板/用户区为展示件（features/chat/*）。
// 用户体系（docs/01 §3.1）：会话「服务端主」；游客不调会话 API（纯内存，关页即失）。

import { useCallback, useEffect, useState } from "react";
import { clearAuth, getUser, type AuthUser } from "@/lib/auth";
import { logout, postFeedback } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import type { ChatMessage, Conversation } from "@/lib/types";
import Composer from "@/components/Composer";
import SettingsDialog from "@/components/SettingsDialog";
import ConfirmDialog from "@/components/ConfirmDialog";
import { useTheme } from "@/features/chat/useTheme";
import { useCapabilities } from "@/features/chat/useCapabilities";
import { useChatSessions } from "@/features/chat/useChatSessions";
import { useChatStream } from "@/features/chat/useChatStream";
import { exportChatMarkdown } from "@/features/chat/exportMarkdown";
import Sidebar from "@/features/chat/Sidebar";
import ChatPanel from "@/features/chat/ChatPanel";
import UserBox from "@/features/chat/UserBox";

export default function Home() {
  const { lang, t } = useI18n();

  // 初始化即校验登录态：未登录/会话失效 → /login（由 getUser 与 401 跳转共同兜底）。
  // getUser SSR 安全（try/catch）；跳转必须在 useEffect（预渲染期无 window，C7 实证）。
  const [authUser] = useState<AuthUser | null>(() => getUser());
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
    if (!getUser()) {
      window.location.assign("/login");
    }
  }, []);
  const isGuest = authUser?.role === "guest";

  // 开关状态 hoist 本层（Composer 开关与 stream payload 共享；值直传 hook，无过期闭包）
  const [webEnabled, setWebEnabled] = useState(false);
  const [thinkingEnabled, setThinkingEnabled] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<Conversation | null>(null);
  const [confirmBatch, setConfirmBatch] = useState(false);

  const { themeMode, setThemeMode } = useTheme();
  const caps = useCapabilities(!!isGuest);
  const sessions = useChatSessions(authUser);
  const stream = useChatStream(authUser, sessions, webEnabled, thinkingEnabled,
                               caps.provider, lang === "en" ? "en" : "zh");
  const { active } = sessions;

  const handleLogout = useCallback(async () => {
    try {
      await logout();
    } catch {
      /* 服务端不可达也要能退出 */
    }
    clearAuth();
    window.location.assign("/login");
  }, []);

  // A7 反馈：点赞/点踩 + 回传该轮来源 + 来源归属标记（G4 点踩必评）
  const handleFeedback = useCallback(
    async (message: ChatMessage, rating: "up" | "down") => {
      if (!active) return;
      sessions.updateConvo(active.id, (c) => ({
        ...c,
        messages: c.messages.map((m) => (m.id === message.id ? { ...m, feedback: rating } : m)),
      }));
      try {
        await postFeedback({
          conversation_id: active.id,
          question: [...active.messages].reverse().find((m) => m.role === "user")?.content ?? "",
          rating,
          sources: (message.sources ?? []).map((s) => ({ id: s.id, title: s.title })),
          trace_id: message.done?.trace_id ?? "",
          sources_belongs_to_round: true,
          answer: message.content,
        });
      } catch {
        /* 反馈失败不阻塞界面 */
      }
    },
    [active, sessions],
  );

  if (!mounted || !authUser) {
    return null; // 预渲染/首帧占位（防 hydration 错配）；未登录由 useEffect 跳转
  }

  return (
    <div className="flex h-dvh bg-base text-fg">
      <Sidebar
        authUser={authUser}
        canExport={!!active?.messages.length}
        onOpenSettings={() => setSettingsOpen(true)}
        onExport={() => active && exportChatMarkdown(active, t)}
        onLogout={handleLogout}
        activeId={sessions.activeId}
        searchText={sessions.searchText}
        filteredConvos={sessions.filteredConvos}
        healthLabel={
          caps.healthOk === null ? t.healthChecking
            : caps.healthOk ? t.healthOk : t.healthBad
        }
        selecting={sessions.selecting}
        selectedIds={sessions.selectedIds}
        renamingId={sessions.renamingId}
        renameText={sessions.renameText}
        onNewChat={sessions.newChat}
        onToggleSelect={() =>
          (sessions.selecting ? sessions.exitSelect() : sessions.setSelecting(true))}
        onSearch={sessions.setSearchText}
        onSelectAll={sessions.selectAllVisible}
        onDeleteSelected={() => setConfirmBatch(true)}
        onExitSelect={sessions.exitSelect}
        onOpen={sessions.openConversation}
        onToggleSelectRow={sessions.toggleSelect}
        onRenameStart={(c) => {
          sessions.setRenamingId(c.id);
          sessions.setRenameText(c.title === "新会话" ? "" : c.title);
        }}
        onRenameText={sessions.setRenameText}
        onRenameCommit={sessions.commitRename}
        onRenameCancel={() => sessions.setRenamingId(null)}
        onDeleteAsk={(c) => setConfirmDelete(c)}
      />

      {/* 主区 */}
      <main className="flex min-w-0 flex-1 flex-col">
        <ChatPanel
          messages={active?.messages ?? []}
          streaming={stream.streaming}
          onFeedback={handleFeedback}
        />
        <Composer
          disabled={caps.healthOk === false}
          streaming={stream.streaming}
          webEnabled={webEnabled}
          webReady={caps.webReady}
          thinkingEnabled={thinkingEnabled}
          visionReady={caps.visionReady && !isGuest} // 游客禁传图（服务端 vision 403 双保险）
          attachment={stream.attachment}
          uploadingImage={stream.uploadingImage}
          onToggleWeb={() => setWebEnabled((v) => !v)}
          onToggleThinking={() => setThinkingEnabled((v) => !v)}
          onPickFile={stream.pickFile}
          onRemoveAttachment={() => stream.setAttachment(null)}
          onSend={stream.send}
          onStop={stream.stop}
        />
      </main>

      {settingsOpen && (
        <SettingsDialog
          themeMode={themeMode}
          onTheme={setThemeMode}
          providers={isGuest ? null : caps.providers} // 游客锁默认档：隐藏下拉
          provider={caps.provider}
          onProvider={caps.setProvider}
          onClose={() => setSettingsOpen(false)}
        />
      )}
      {confirmDelete && (
        <ConfirmDialog
          title={t.deleteConvTitle}
          body={t.convDeleteBody(confirmDelete.title === "新会话" ? t.newChat : confirmDelete.title)}
          confirmLabel={t.delete}
          cancelLabel={t.cancel}
          onConfirm={() => sessions.removeChat(confirmDelete.id)}
          onClose={() => setConfirmDelete(null)}
        />
      )}
      {confirmBatch && (
        <ConfirmDialog
          title={t.batchDeleteTitle}
          body={t.batchDeleteBody(sessions.selectedIds.size)}
          confirmLabel={t.delete}
          cancelLabel={t.cancel}
          onConfirm={sessions.doBatchRemove}
          onClose={() => setConfirmBatch(false)}
        />
      )}
      {/* 导出（WP4）：当前会话 → .md（游客隐藏入口，见 UserBox） */}
      <span className="hidden">
        <button onClick={() => active && exportChatMarkdown(active, t)}>export</button>
      </span>
    </div>
  );
}
