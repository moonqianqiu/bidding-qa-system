"use client";

// 问答主页（docs/02 A1-A9）——薄壳（C7 拆分后只剩状态声明与布局组装）：
// 会话状态 useChatSessions / 问答流 useChatStream / 能力探测 useCapabilities /
// 主题 useTheme；侧栏/消息面板/用户区为展示件（features/chat/*）。
// 用户体系（docs/01 §3.1）：会话「服务端主」；游客不调会话 API（纯内存，关页即失）。

import { useCallback, useEffect, useState } from "react";
import { clearAuth, getUser, setAuth, type AuthUser } from "@/lib/auth";
import { getMe, logout, postFeedback } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import type { ChatMessage, Conversation } from "@/lib/types";
import Composer from "@/components/Composer";
import SettingsDialog from "@/components/SettingsDialog";
import ConfirmDialog from "@/components/ConfirmDialog";
import { useTheme } from "@/features/chat/useTheme";
import { useCapabilities } from "@/features/chat/useCapabilities";
import { useChatSessions } from "@/features/chat/useChatSessions";
import { useChatStream } from "@/features/chat/useChatStream";
import { useImageAttachment, type DocMode } from "@/features/chat/useImageAttachment";
import { exportChatMarkdown } from "@/features/chat/exportMarkdown";
import Sidebar from "@/features/chat/Sidebar";
import ChatPanel from "@/features/chat/ChatPanel";
import UserBox from "@/features/chat/UserBox";

export default function Home() {
  const { lang, t } = useI18n();

  // 初始化即校验登录态：未登录 → /login；**单机模式（users_enabled=false）→ 注入
  // 占位身份放行**（审计 P0-5：原主页查 localStorage 无身份即跳 /login，登录页又因
  // 单机模式跳回 / → 死循环整站不可用）。getUser SSR 安全；跳转必须在 useEffect。
  const [authUser, setAuthUser] = useState<AuthUser | null>(() => getUser());
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
    if (getUser()) return; // 已登录（含游客）
    getMe()
      .then((m) => {
        if (m.users_enabled) {
          window.location.assign("/login");
          return;
        }
        // 单机模式无登录概念：本地占位身份（后端 middleware 单机分支放行，
        // 与 token 无关）；role=user 放开导出/传图等 UI 门控
        setAuth("", { username: "local", role: "user" });
        setAuthUser({ username: "local", role: "user" });
      })
      .catch(() => window.location.assign("/login")); // 后端不可达：登录页展示错误
  }, []);
  const isGuest = authUser?.role === "guest";

  // 开关状态 hoist 本层（Composer 开关与 stream payload 共享；值直传 hook，无过期闭包）
  const [webEnabled, setWebEnabled] = useState(false);
  const [thinkingEnabled, setThinkingEnabled] = useState(false);
  const [docMode, setDocMode] = useState<DocMode>("auto"); // v2.3：关=Qwen 自动路由，开=按文档解析
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<Conversation | null>(null);
  const [confirmBatch, setConfirmBatch] = useState(false);

  const { themeMode, setThemeMode } = useTheme();
  const caps = useCapabilities(!!isGuest);
  const sessions = useChatSessions(authUser);
  const stream = useChatStream(authUser, sessions, webEnabled, thinkingEnabled,
                               caps.provider, lang === "en" ? "en" : "zh");
  const images = useImageAttachment();
  const { active } = sessions;

  // v2.3 双通道：发送前先解析图片换 image_ref（失败=不带图继续，chip 留错态）
  const handleSend = async (text: string) => {
    const ref = await images.prepareForSend(docMode);
    await stream.send(text, ref ? [ref] : undefined);
  };

  const handleLogout = useCallback(async () => {
    try {
      await logout();
    } catch {
      /* 服务端不可达也要能退出 */
    }
    clearAuth();
    window.location.assign("/login");
  }, []);

  // A7 反馈：点赞/点踩 + 回传该轮来源 + 来源归属标记（G4 点踩必评）。
  // P1-23（审计）：question 取「该 assistant 前最近的 user 消息」（与回放侧
  // storedToMessages 同口径）——原取全会话最后一个 user 问题，非最后一轮点踩必错配；
  // 乐观置态失败回滚（原吞错后 UI 与服务端永久不一致）。
  const handleFeedback = useCallback(
    async (message: ChatMessage, rating: "up" | "down") => {
      if (!active) return;
      const idx = active.messages.findIndex((m) => m.id === message.id);
      const question = idx >= 0
        ? [...active.messages.slice(0, idx)].reverse().find((m) => m.role === "user")?.content ?? ""
        : "";
      const prevRating = message.feedback ?? null;
      sessions.updateConvo(active.id, (c) => ({
        ...c,
        messages: c.messages.map((m) => (m.id === message.id ? { ...m, feedback: rating } : m)),
      }));
      try {
        await postFeedback({
          conversation_id: active.id,
          question,
          rating,
          sources: (message.sources ?? []).map((s) => ({ id: s.id, title: s.title })),
          trace_id: message.done?.trace_id ?? "",
          sources_belongs_to_round: true,
          answer: message.content,
        });
      } catch {
        sessions.updateConvo(active.id, (c) => ({
          ...c,
          messages: c.messages.map((m) =>
            m.id === message.id ? { ...m, feedback: prevRating } : m),
        }));
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
          visionOcrReady={caps.visionOcrReady && !isGuest}
          attachment={images.attachment}
          preparing={images.preparing}
          docMode={docMode}
          onToggleWeb={() => setWebEnabled((v) => !v)}
          onToggleThinking={() => setThinkingEnabled((v) => !v)}
          onToggleDocMode={() => setDocMode((m) => (m === "ocr" ? "auto" : "ocr"))}
          onPickFile={images.pickFile}
          onRemoveAttachment={images.remove}
          onSend={handleSend}
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
