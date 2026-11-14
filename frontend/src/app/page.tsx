"use client";

// 问答主页（docs/02 A1-A9）：会话侧栏 + 消息流 + 输入区三区布局。
// 连接管理（A8）：AbortController + 请求序号防旧流覆盖；中断保留已流内容。
// A1 双主题三档 + i18n（LangCtx 由 layout 的 Providers 提供，/login 同源可用）+
// 设置对话框 + 删除确认弹窗。
// 用户体系（docs/01 §3.1）：会话「服务端主」——列表/详情来自 /api/conversations，
// localStorage 只存主题/语言/档位/凭证；游客不调会话 API（纯内存，关页即失）。

import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatMessage, Conversation, SSEEvent } from "@/lib/types";
import { makeSeqGuard, streamChat } from "@/lib/sse";
import {
  deleteConversation, getConversation, getHealth, getProviders, listConversations,
  logout, postFeedback, postVision, renameConversation,
  type ConvSummary, type Providers,
} from "@/lib/api";
import { clearAuth, getUser, type AuthUser } from "@/lib/auth";
import { compressImage } from "@/lib/image";
import { useI18n } from "@/lib/i18n";
import Composer, { type Attachment } from "@/components/Composer";
import MessageItem from "@/components/MessageItem";
import SettingsDialog, { type ThemeMode } from "@/components/SettingsDialog";
import ConfirmDialog from "@/components/ConfirmDialog";

const LS_THEME = "bidqa.theme";

/** 服务端会话摘要 → 前端会话（messages 惰性：openConversation 时按需拉详情）。 */
function toConversation(s: ConvSummary): Conversation {
  return { id: s.id, title: s.title, updatedAt: Date.parse(s.updated_at) || 0, messages: [] };
}

/** 服务端 items（_persist 落库：role/content 二元）→ 前端消息（正文回放；来源/图表不入库）。 */
function storedToMessages(items: { role: "user" | "assistant"; content: string }[]): ChatMessage[] {
  return items.map((m, i) => ({
    id: `srv-${i}`, role: m.role, content: m.content, ts: 0,
  }));
}

/** 纯函数版「补丁到最后一条未完成助手消息」（流式渲染路径共用）。 */
function patchLastAssistantIn(
  c: Conversation,
  patch: (m: ChatMessage) => ChatMessage,
): Conversation {
  const messages = [...c.messages];
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === "assistant" && !messages[i].done && !messages[i].error) {
      messages[i] = patch(messages[i]);
      break;
    }
  }
  return { ...c, messages, updatedAt: Date.now() };
}

function newConversation(): Conversation {
  return {
    id: crypto.randomUUID(),
    title: "新会话",
    updatedAt: Date.now(),
    messages: [],
  };
}

function readThemeMode(): ThemeMode {
  try {
    const t = localStorage.getItem(LS_THEME);
    return t === "dark" || t === "light" ? t : "system";
  } catch {
    return "system";
  }
}

/** 把主题档位落到 <html> class 并持久化；system 档按当前 OS 解析。 */
function applyTheme(mode: ThemeMode) {
  let dark = mode === "dark";
  if (mode === "system") {
    try {
      dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    } catch {
      dark = true;
    }
  }
  document.documentElement.classList.toggle("dark", dark);
  try {
    localStorage.setItem(LS_THEME, mode);
  } catch {
    /* 隐私模式等场景静默 */
  }
}

export default function Home() {
  const [convos, setConvos] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [webEnabled, setWebEnabled] = useState(false);
  const [thinkingEnabled, setThinkingEnabled] = useState(false);
  const [healthOk, setHealthOk] = useState<boolean | null>(null);
  const [visionReady, setVisionReady] = useState(false); // A3：vision 就绪才显示上传按钮
  const [attachment, setAttachment] = useState<Attachment | null>(null);
  const [uploadingImage, setUploadingImage] = useState(false);
  const [themeMode, setThemeModeState] = useState<ThemeMode>("system");
  const [providers, setProviders] = useState<Providers | null>(null);
  const [provider, setProviderState] = useState(""); // "" = 默认槽位（A12）
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<Conversation | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null); // WP4 重命名
  const [renameText, setRenameText] = useState("");
  const [webReady, setWebReady] = useState(false); // D4/D5：联网供应商已配置才显示联网开关
  const [searchText, setSearchText] = useState(""); // A13 会话搜索（前端本地过滤）
  const [authUser, setAuthUser] = useState<AuthUser | null>(null); // 用户体系：当前身份
  const [selecting, setSelecting] = useState(false); // 2026-11-08 批量清理：多选模式
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [confirmBatch, setConfirmBatch] = useState(false);
  const { lang, t, setLang } = useI18n();

  const abortRef = useRef<AbortController | null>(null);
  const requestSeqRef = useRef(0); // A8 请求序号：旧流事件不得覆盖新会话
  const bottomRef = useRef<HTMLDivElement>(null);
  // WP3 打字机平滑：token/thinking 先入缓冲，rAF 合帧 flush（~30fps）。
  // 量测依据（2026-11-08）：后端 token 到达 p50=75ms 相对平滑，卡顿主因是每 token
  // 全量渲染；用户体系后会话改服务端主，localStorage 落盘逻辑整体移除。
  const pendingRef = useRef({ content: "", thinking: "" });
  const rafRef = useRef<number | null>(null);
  const lastFlushRef = useRef(0);
  const convosRef = useRef<Conversation[]>([]);
  useEffect(() => {
    convosRef.current = convos;
  }, [convos]);

  // 初始化：登录态校验 → 服务端会话列表（游客跳过）+ 健康检查 + 主题/档位恢复。
  // 未登录/会话失效 → /login（由 getMe 与 jfetch 的 401 跳转共同兜底）。
  useEffect(() => {
    const user = getUser();
    if (!user) {
      window.location.assign("/login");
      return;
    }
    setAuthUser(user);
    const isGuest = user.role === "guest";
    if (isGuest) {
      const c = newConversation(); // 游客：纯内存会话（不落库，关页即失）
      setConvos([c]);
      setActiveId(c.id);
    } else {
      listConversations()
        .then((r) => {
          const convs = r.conversations.map(toConversation);
          const next = convs.length ? convs : [newConversation()];
          setConvos(next);
          setActiveId(next[0].id);
        })
        .catch(() => {
          const c = newConversation(); // 列表拉取失败不阻塞使用（PG 挂时后端 5031）
          setConvos([c]);
          setActiveId(c.id);
        });
    }
    getHealth()
      .then((h) => {
        setHealthOk(h.status === "ok");
        setVisionReady(h.capabilities?.vision === true);
        setWebReady(h.capabilities?.web_search === true); // 留空不开启=按钮不出现（复用 vision 门控模式）
      })
      .catch(() => setHealthOk(false));
    const mode = readThemeMode();
    setThemeModeState(mode);
    applyTheme(mode);
    try {
      setProviderState(localStorage.getItem("bidqa.provider") ?? "");
    } catch {
      /* 静默 */
    }
    if (!isGuest) getProviders().then(setProviders).catch(() => setProviders(null)); // 游客锁默认档=不拉取
  }, []);

  // 跟随系统档：OS 深浅切换时即时跟随（用户在系统设置里换主题，页面跟着变）
  useEffect(() => {
    if (themeMode !== "system") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => applyTheme("system");
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [themeMode]);

  const setThemeMode = useCallback((mode: ThemeMode) => {
    setThemeModeState(mode);
    applyTheme(mode); // 即时生效 + 持久化（不"只改设置框"）
  }, []);

  const setProvider = useCallback((name: string) => {
    setProviderState(name);
    try {
      localStorage.setItem("bidqa.provider", name); // "" 表示默认槽位
    } catch {
      /* 静默 */
    }
  }, []);

  const active = convos.find((c) => c.id === activeId) ?? null;

  // A13 会话搜索：标题或任一消息内容命中（大小写不敏感；保留现有数组序）。
  // 置于回调之前：批量清理的「全选」引用 filteredConvos。
  const q = searchText.trim().toLowerCase();
  const filteredConvos = q
    ? convos.filter((c) => c.title.toLowerCase().includes(q)
        || c.messages.some((m) => m.content.toLowerCase().includes(q)))
    : convos;

  // 服务端主：流式期间只改内存状态；落库由后端 _persist 完成，done 后刷新列表取回服务端标题。
  const refreshList = useCallback(() => {
    if (authUser?.role === "guest") return; // 游客不调会话 API
    listConversations()
      .then((r) => {
        setConvos((prev) => {
          const prevMap = new Map(prev.map((c) => [c.id, c]));
          return r.conversations.map((s) => {
            const local = prevMap.get(s.id);
            // 本地内存里有正文的（正在浏览/刚流完）保留本地 messages；否则惰性等点开再拉
            return local
              ? { ...local, title: s.title || local.title,
                  updatedAt: Date.parse(s.updated_at) || local.updatedAt }
              : toConversation(s);
          });
        });
      })
      .catch(() => {}); // 列表刷新失败不打断使用
  }, [authUser]);

  // 打开会话：无正文的（服务端来的摘要）按需拉详情回放正文
  const openConversation = useCallback(
    (id: string) => {
      setActiveId(id);
      const target = convosRef.current.find((c) => c.id === id);
      if (!target || target.messages.length || authUser?.role === "guest") return;
      getConversation(id)
        .then((d) => {
          setConvos((cur) => cur.map((c) => (c.id === id && c.messages.length === 0
            ? { ...c, messages: storedToMessages(d.messages.items) } : c)));
        })
        .catch(() => {});
    },
    [authUser],
  );

  const updateConvo = useCallback(
    (id: string, updater: (c: Conversation) => Conversation) => {
      setConvos((prev) => prev.map((c) => (c.id === id ? updater(c) : c)));
    },
    [],
  );

  const patchLastAssistant = useCallback(
    (convoId: string, patch: (m: ChatMessage) => ChatMessage) => {
      updateConvo(convoId, (c) => patchLastAssistantIn(c, patch));
    },
    [updateConvo],
  );

  // 合帧 flush：把缓冲增量打进最后一条助手消息（~30fps 渲染合帧，非持久化）
  const flushPending = useCallback(
    (convoId: string) => {
      const { content, thinking } = pendingRef.current;
      if (!content && !thinking) return;
      pendingRef.current = { content: "", thinking: "" };
      patchLastAssistant(convoId, (m) => ({
        ...m,
        content: m.content + content,
        thinking: (m.thinking ?? "") + thinking,
        statusStage: content ? undefined : m.statusStage,
      }));
    },
    [patchLastAssistant],
  );

  const startFlusher = useCallback(
    (convoId: string) => {
      if (rafRef.current != null) return;
      lastFlushRef.current = 0;
      const tick = (t: number) => {
        if (t - lastFlushRef.current >= 33) { // ~30fps 合帧：后端 p50 75ms，同帧多 token 合并渲染
          lastFlushRef.current = t;
          flushPending(convoId);
        }
        rafRef.current = requestAnimationFrame(tick);
      };
      rafRef.current = requestAnimationFrame(tick);
    },
    [flushPending],
  );

  const stopFlusher = useCallback(() => {
    if (rafRef.current != null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
  }, []);

  const pickFile = useCallback(
    async (file: File) => {
      if (uploadingImage) return;
      setUploadingImage(true);
      setAttachment(null);
      try {
        const image = await compressImage(file); // 长边≤1024 / JPEG 0.85 / 白底（A3）
        const r = await postVision(image, file.name);
        setAttachment({ name: file.name, description: r.description, ref: r.image_ref });
      } catch {
        setAttachment({ name: file.name, description: "", error: true }); // 5031/413/网络，统一错误 chip
      } finally {
        setUploadingImage(false);
      }
    },
    [uploadingImage],
  );

  const send = useCallback(
    async (text: string) => {
      if (!active || streaming) return;
      const imageRefs = attachment?.ref ? [attachment.ref] : undefined;
      setAttachment(null); // 附件随本轮消耗（描述 TTL 10min，不跨轮复用）
      const convoId = active.id;
      const requestSeq = ++requestSeqRef.current;
      const userMsg: ChatMessage = {
        id: crypto.randomUUID(), role: "user", content: text, ts: Date.now(),
      };
      const assistantMsg: ChatMessage = {
        id: crypto.randomUUID(), role: "assistant", content: "", ts: Date.now(),
      };
      updateConvo(convoId, (c) => ({
        ...c,
        title: c.messages.length === 0 ? text.slice(0, 20) : c.title,
        messages: [...c.messages, userMsg, assistantMsg],
        updatedAt: Date.now(),
      }));
      setStreaming(true);
      const guard = makeSeqGuard();
      const controller = new AbortController();
      abortRef.current = controller;
      pendingRef.current = { content: "", thinking: "" }; // WP3：新流清缓冲
      startFlusher(convoId);

      await streamChat(
        {
          conversation_id: convoId,
          message: text,
          web_enabled: webEnabled,
          thinking_enabled: thinkingEnabled,
          image_refs: imageRefs,
          provider: provider || null,
          client_msg_id: userMsg.id,
          lang: lang === "en" ? "en" : "zh", // P1：界面语言 → 回答语言
        },
        {
          onEvent: (event: SSEEvent) => {
            if (requestSeqRef.current !== requestSeq) return; // 旧流，丢弃（A8）
            if (!guard(event)) return; // 乱序/重复，丢弃（A4）
            switch (event.type) {
              case "status":
                patchLastAssistant(convoId, (m) => ({ ...m, statusStage: event.stage ?? "" }));
                break;
              case "thinking":
                pendingRef.current.thinking += (event.text ?? ""); // 缓冲，rAF 合帧渲染
                break;
              case "token":
                pendingRef.current.content += (event.text ?? ""); // 缓冲，rAF 合帧渲染
                break;
              case "reset": // A9：清空已流正文与队列，等待 status 重新进入 token
                pendingRef.current = { content: "", thinking: "" };
                patchLastAssistant(convoId, (m) => ({ ...m, content: "", thinking: "" }));
                break;
              case "done":
                flushPending(convoId); // 同步 flush 残余
                patchLastAssistant(convoId, (m) => ({
                  ...m,
                  done: event,
                  sources: event.clarify ? [] : event.sources,
                  charts: event.charts,
                  statusStage: undefined,
                }));
                refreshList(); // 服务端已在 done 前落库：取回服务端标题/排序
                break;
              case "error":
                flushPending(convoId);
                patchLastAssistant(convoId, (m) => ({
                  ...m, error: `[${event.error_code}] ${event.message ?? "未知错误"}`,
                  statusStage: undefined,
                }));
                break;
              default: // ping 忽略（A4）
                break;
            }
          },
          onError: (err: Error) => {
            if (requestSeqRef.current !== requestSeq) return;
            flushPending(convoId); // 中断/断连：残余正文保留在内存
            patchLastAssistant(convoId, (m) => ({ ...m, error: err.message }));
          },
        },
        controller.signal,
      ).finally(() => {
        stopFlusher();
        if (requestSeqRef.current === requestSeq) {
          flushPending(convoId); // 收尾残余 flush（done/error 路径通常已 flush 空）
          setStreaming(false);
          abortRef.current = null;
          // 流结束仍未 done/error（中断/断连）→ 标记保留（A8）
          patchLastAssistant(convoId, (m) =>
            m.done || m.error ? m : { ...m, interrupted: true, statusStage: undefined });
        }
      });
    },
    [active, streaming, webEnabled, thinkingEnabled, provider, attachment, lang,
     updateConvo, patchLastAssistant, flushPending, startFlusher, stopFlusher, refreshList],
  );

  const stop = useCallback(() => {
    abortRef.current?.abort(); // 中断保留已流内容（A8）
  }, []);

  // WP4 重命名：Enter 提交（本地 + 服务端 PATCH），Esc 取消；失败静默（与删除同款语义）
  const commitRename = useCallback(async () => {
    const id = renamingId;
    const title = renameText.trim();
    setRenamingId(null);
    if (!id || !title) return;
    updateConvo(id, (c) => ({ ...c, title }));
    try {
      await renameConversation(id, title);
    } catch {
      /* 本地会话可能没有服务端记录 */
    }
  }, [renamingId, renameText, updateConvo]);

  // WP4 导出 Markdown：当前会话 → .md 下载（标题 + 问答分段 + 来源清单）
  const exportChat = useCallback(() => {
    if (!active) return;
    const lines: string[] = [`# ${active.title}`, ""];
    for (const m of active.messages) {
      lines.push(m.role === "user" ? `## ${t.exportQ}` : `## ${t.exportA}`, "", m.content, "");
      if (m.sources?.length) {
        lines.push(`**${t.sourcesCount(m.sources.length)}**`);
        lines.push(...m.sources.map((s, i) =>
          `${i + 1}. ${s.title}${s.url ? ` (${s.url})` : ""}`), "");
      }
    }
    const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${active.title || "conversation"}.md`;
    a.click();
    URL.revokeObjectURL(url);
  }, [active, t]);

  const newChat = useCallback(() => {
    const c = newConversation();
    setConvos((prev) => [c, ...prev]); // 服务端落库发生在首轮 done（_persist）
    setActiveId(c.id);
  }, []);

  const removeChat = useCallback(
    async (id: string) => {
      setConvos((prev) => {
        let next = prev.filter((c) => c.id !== id);
        if (next.length === 0) next = [newConversation()]; // 至少保留一个会话
        setActiveId((cur) => (next.some((c) => c.id === cur) ? cur : next[0].id));
        return next;
      });
      if (authUser?.role === "guest") return; // 游客无服务端会话（纯内存，删除只动本地）
      try {
        await deleteConversation(id); // 服务端有就删（404 忽略）
      } catch {
        /* 未发送过消息的会话服务端无记录 */
      }
    },
    [authUser],
  );

  // 用户体系：退出登录（服务端吊销会话 + 清本地态 → 登录页）
  const handleLogout = useCallback(async () => {
    try {
      await logout();
    } catch {
      /* 服务端不可达也要能退出 */
    }
    clearAuth();
    window.location.assign("/login");
  }, []);

  // ── 批量清理（多选删除）：选中→确认→逐条删服务端+一次性出本地列表 ──
  const toggleSelect = useCallback((id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const selectAllVisible = useCallback(() => {
    setSelectedIds(new Set(filteredConvos.map((c) => c.id)));
  }, [filteredConvos]);

  const exitSelect = useCallback(() => {
    setSelecting(false);
    setSelectedIds(new Set());
  }, []);

  const doBatchRemove = useCallback(async () => {
    const ids = [...selectedIds];
    setConfirmBatch(false);
    setSelecting(false);
    setSelectedIds(new Set());
    setConvos((prev) => {
      let next = prev.filter((c) => !ids.includes(c.id));
      if (next.length === 0) next = [newConversation()]; // 至少保留一个会话
      setActiveId((cur) => (next.some((c) => c.id === cur) ? cur : next[0].id));
      return next;
    });
    if (authUser?.role === "guest") return; // 游客：纯内存，本地即完成
    for (const id of ids) {
      try {
        await deleteConversation(id); // 服务端无记录（未发送过）→ 404 静默
      } catch {
        /* 与单删同款：失败静默不阻塞 */
      }
    }
    refreshList();
  }, [selectedIds, authUser, refreshList]);

  // A7 反馈：点赞/点踩 + 回传该轮来源 + 来源归属标记
  const handleFeedback = useCallback(
    async (message: ChatMessage, rating: "up" | "down") => {
      if (!active) return;
      updateConvo(active.id, (c) => ({
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
          answer: message.content, // G4 点踩必评（忠实度判分）
        });
      } catch {
        /* 反馈失败不阻塞界面 */
      }
    },
    [active, updateConvo],
  );

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [active?.messages.length, streaming]);

  return (
    <div className="flex h-dvh bg-base text-fg">
        {/* 会话侧栏（A1/A2） */}
        <aside className="hidden w-60 shrink-0 flex-col border-r border-line/80 bg-panel/40 md:flex">
          <div className="p-3">
            <h1 className="text-sm font-semibold text-fg">{t.appName}</h1>
            <p className="mt-0.5 text-[10px] text-faint">
              {healthOk === null ? t.healthChecking : healthOk ? t.healthOk : t.healthBad}
            </p>
          </div>
          <div className="mx-3 flex gap-1.5">
            <button
              onClick={newChat}
              className="flex-1 rounded-lg border border-line py-1.5 text-xs text-dim hover:border-sky-800 hover:text-sky-300"
            >
              {t.newChatBtn}
            </button>
            {/* 批量清理入口（2026-11-08）：进入多选模式 */}
            <button
              onClick={() => (selecting ? exitSelect() : setSelecting(true))}
              title={t.multiSelect}
              className={`rounded-lg border px-2.5 py-1.5 text-xs ${
                selecting
                  ? "border-sky-800 bg-sky-950/40 text-sky-300"
                  : "border-line text-dim hover:border-sky-800 hover:text-sky-300"
              }`}
            >
              ☑
            </button>
          </div>
          {/* A13 会话搜索：标题+消息关键词本地过滤（docs/02 A13，M4 后演进服务端检索） */}
          <input
            value={searchText}
            onChange={(e) => setSearchText(e.target.value)}
            placeholder={t.searchConvos}
            className="mx-3 mt-2 w-[calc(100%-1.5rem)] rounded-lg border border-line bg-panel px-2 py-1.5 text-xs text-fg outline-none placeholder:text-faint focus:border-sky-800"
          />
          {/* 多选模式操作条：计数 / 全选（限可见项）/ 删除所选 / 取消 */}
          {selecting && (
            <div className="mx-3 mt-2 flex items-center justify-between gap-1 rounded-lg border border-line bg-panel px-2 py-1.5 text-xs">
              <span className="shrink-0 text-dim">{t.selectedCount(selectedIds.size)}</span>
              <span className="flex shrink-0 gap-2">
                <button onClick={selectAllVisible} className="text-sky-300 hover:text-sky-200">
                  {t.selectAll}
                </button>
                <button
                  onClick={() => setConfirmBatch(true)}
                  disabled={selectedIds.size === 0}
                  className="text-red-300 hover:text-red-200 disabled:opacity-40"
                >
                  {t.deleteSelected}
                </button>
                <button onClick={exitSelect} className="text-faint hover:text-fg">
                  {t.cancel}
                </button>
              </span>
            </div>
          )}
          <div className="mt-2 flex-1 space-y-0.5 overflow-y-auto px-2 pb-3">
            {filteredConvos.length === 0 && (
              <p className="px-2.5 py-2 text-xs text-faint">{t.noConvMatch}</p>
            )}
            {filteredConvos.map((c) => (
              <div
                key={c.id}
                className={`group flex cursor-pointer items-center rounded-lg px-2.5 py-2 text-xs ${
                  selecting
                    ? selectedIds.has(c.id)
                      ? "bg-sky-950/40 text-fg"
                      : "text-dim hover:bg-panel"
                    : c.id === activeId
                      ? "bg-line/80 text-fg"
                      : "text-dim hover:bg-panel"
                }`}
                onClick={() => (selecting ? toggleSelect(c.id) : openConversation(c.id))}
              >
                {selecting && (
                  <span
                    className={`mr-1.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border text-[10px] leading-none ${
                      selectedIds.has(c.id)
                        ? "border-sky-600 bg-sky-800 text-white"
                        : "border-line text-transparent"
                    }`}
                  >
                    ✓
                  </span>
                )}
                {renamingId === c.id ? (
                  <input
                    autoFocus
                    className="min-w-0 flex-1 rounded border border-line bg-panel px-1 py-0.5 text-xs text-fg outline-none"
                    value={renameText}
                    placeholder={t.renamePlaceholder}
                    onClick={(e) => e.stopPropagation()}
                    onChange={(e) => setRenameText(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") commitRename();
                      if (e.key === "Escape") setRenamingId(null);
                    }}
                    onBlur={commitRename}
                  />
                ) : (
                  <span className="min-w-0 flex-1 truncate">{c.title === "新会话" ? t.newChat : c.title}</span>
                )}
                {!selecting && renamingId !== c.id && (
                  <button
                    className="ml-1 hidden text-faint hover:text-fg group-hover:block"
                    title={t.rename}
                    onClick={(e) => {
                      e.stopPropagation();
                      setRenamingId(c.id);
                      setRenameText(c.title === "新会话" ? "" : c.title);
                    }}
                  >
                    ✏️
                  </button>
                )}
                {!selecting && (
                  <button
                    className="ml-1 hidden text-faint hover:text-red-400 group-hover:block"
                    title={t.deleteConvTitle}
                    onClick={(e) => {
                      e.stopPropagation();
                      setConfirmDelete(c); // 先确认再删（防手滑）
                    }}
                  >
                    ✕
                  </button>
                )}
              </div>
            ))}
          </div>
          {/* 设置入口 + 导出（游客隐藏）+ 当前用户（docs/01 §3.1 角色门控） */}
          <div className="border-t border-line/80 p-3">
            <button
              onClick={() => setSettingsOpen(true)}
              className="flex w-full items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs text-dim hover:bg-panel hover:text-fg"
            >
              ⚙ {t.settings}
            </button>
            {authUser?.role !== "guest" && (
              <button
                onClick={exportChat}
                disabled={!active?.messages.length}
                className="flex w-full items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs text-dim hover:bg-panel hover:text-fg disabled:opacity-40"
              >
                ⬇ {t.exportChat}
              </button>
            )}
            {authUser?.role === "admin" && (
              <a
                href="/console"
                className="flex w-full items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs text-dim hover:bg-panel hover:text-fg"
              >
                ▦ {t.consoleTitle}
              </a>
            )}
            {authUser && (
              <div className="mt-1 flex items-center justify-between gap-1 rounded-lg px-2 py-1.5 text-xs">
                <span className="flex min-w-0 items-center gap-1.5">
                  <span className="truncate text-dim">{authUser.username}</span>
                  {authUser.role === "guest" && (
                    <span className="shrink-0 rounded bg-amber-950/60 px-1.5 py-0.5 text-[10px] text-amber-300">
                      {t.guestBadge}
                    </span>
                  )}
                  {authUser.role === "admin" && (
                    <span className="shrink-0 rounded bg-sky-950/60 px-1.5 py-0.5 text-[10px] text-sky-300">
                      admin
                    </span>
                  )}
                </span>
                <button
                  onClick={handleLogout}
                  title={t.logout}
                  className="shrink-0 text-faint hover:text-fg"
                >
                  ⎋
                </button>
              </div>
            )}
          </div>
        </aside>

        {/* 主区 */}
        <main className="flex min-w-0 flex-1 flex-col">
          <div className="flex-1 overflow-y-auto px-4 py-6">
            <div className="mx-auto max-w-3xl space-y-6">
              {!active?.messages.length && (
                <div className="mt-24 text-center">
                  <h2 className="text-2xl font-semibold text-fg">{t.heroTitle}</h2>
                  <p className="mt-2 text-sm text-faint">{t.heroSub}</p>
                </div>
              )}
              {active?.messages.map((m) => (
                <MessageItem key={m.id} message={m} onFeedback={handleFeedback} />
              ))}
              <div ref={bottomRef} />
            </div>
          </div>
          <Composer
            disabled={healthOk === false}
            streaming={streaming}
            webEnabled={webEnabled}
            webReady={webReady}
            thinkingEnabled={thinkingEnabled}
            visionReady={visionReady && authUser?.role !== "guest"} // 游客禁传图（服务端 vision 403 双保险）
            attachment={attachment}
            uploadingImage={uploadingImage}
            onToggleWeb={() => setWebEnabled((v) => !v)}
            onToggleThinking={() => setThinkingEnabled((v) => !v)}
            onPickFile={pickFile}
            onRemoveAttachment={() => setAttachment(null)}
            onSend={send}
            onStop={stop}
          />
        </main>

        {settingsOpen && (
          <SettingsDialog
            themeMode={themeMode}
            onTheme={setThemeMode}
            providers={authUser?.role === "guest" ? null : providers} // 游客锁默认档：隐藏下拉
            provider={provider}
            onProvider={setProvider}
            onClose={() => setSettingsOpen(false)}
          />
        )}
        {confirmDelete && (
          <ConfirmDialog
            title={t.deleteConvTitle}
            body={t.convDeleteBody(confirmDelete.title === "新会话" ? t.newChat : confirmDelete.title)}
            confirmLabel={t.delete}
            cancelLabel={t.cancel}
            onConfirm={() => removeChat(confirmDelete.id)}
            onClose={() => setConfirmDelete(null)}
          />
        )}
        {confirmBatch && (
          <ConfirmDialog
            title={t.batchDeleteTitle}
            body={t.batchDeleteBody(selectedIds.size)}
            confirmLabel={t.delete}
            cancelLabel={t.cancel}
            onConfirm={doBatchRemove}
            onClose={() => setConfirmBatch(false)}
          />
        )}
    </div>
  );
}
