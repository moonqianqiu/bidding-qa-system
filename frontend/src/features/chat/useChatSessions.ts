"use client";

// 会话状态 hook（A2/A13/WP4 + 2026-11-08 批量清理）：服务端主会话的全部状态与动作。
// 游客=纯内存单会话（不调会话 API）；正文惰性拉取（点开才拉详情）；
// 流式期间只改内存（落库由后端完成，done 后 refreshList 取回服务端标题）。

import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatMessage, Conversation } from "@/lib/types";
import {
  deleteConversation, getConversation, listConversations, renameConversation,
  type ConvSummary,
} from "@/lib/api";
import type { AuthUser } from "@/lib/auth";

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

export function useChatSessions(authUser: AuthUser | null) {
  const isGuest = authUser?.role === "guest";
  const [convos, setConvos] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [searchText, setSearchText] = useState(""); // A13 会话搜索（前端本地过滤）
  const [renamingId, setRenamingId] = useState<string | null>(null); // WP4 重命名
  const [renameText, setRenameText] = useState("");
  const [selecting, setSelecting] = useState(false); // 批量清理：多选模式
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  const convosRef = useRef<Conversation[]>([]);
  useEffect(() => {
    convosRef.current = convos;
  }, [convos]);

  // 初始化：服务端会话列表（游客跳过）；列表拉取失败不阻塞使用（PG 挂时后端 5031）
  useEffect(() => {
    if (!authUser) return;
    if (isGuest) {
      const c = newConversation(); // 游客：纯内存会话（不落库，关页即失）
      setConvos([c]);
      setActiveId(c.id);
      return;
    }
    listConversations()
      .then((r) => {
        const convs = r.conversations.map(toConversation);
        const next = convs.length ? convs : [newConversation()];
        setConvos(next);
        setActiveId(next[0].id);
      })
      .catch(() => {
        const c = newConversation();
        setConvos([c]);
        setActiveId(c.id);
      });
  }, [authUser, isGuest]);

  const active = convos.find((c) => c.id === activeId) ?? null;

  // A13 会话搜索：标题或任一消息内容命中（大小写不敏感；保留现有数组序）
  const q = searchText.trim().toLowerCase();
  const filteredConvos = q
    ? convos.filter((c) => c.title.toLowerCase().includes(q)
        || c.messages.some((m) => m.content.toLowerCase().includes(q)))
    : convos;

  /** 服务端主：流式期间只改内存状态；done 后调用取回服务端标题/排序。 */
  const refreshList = useCallback(() => {
    if (isGuest) return; // 游客不调会话 API
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
  }, [isGuest]);

  // 打开会话：无正文的（服务端来的摘要）按需拉详情回放正文
  const openConversation = useCallback(
    (id: string) => {
      setActiveId(id);
      const target = convosRef.current.find((c) => c.id === id);
      if (!target || target.messages.length || isGuest) return;
      getConversation(id)
        .then((d) => {
          setConvos((cur) => cur.map((c) => (c.id === id && c.messages.length === 0
            ? { ...c, messages: storedToMessages(d.messages.items) } : c)));
        })
        .catch(() => {});
    },
    [isGuest],
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
      if (isGuest) return; // 游客无服务端会话（纯内存，删除只动本地）
      try {
        await deleteConversation(id); // 服务端有就删（404 忽略）
      } catch {
        /* 未发送过消息的会话服务端无记录 */
      }
    },
    [isGuest],
  );

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
    setSelecting(false);
    setSelectedIds(new Set());
    setConvos((prev) => {
      let next = prev.filter((c) => !ids.includes(c.id));
      if (next.length === 0) next = [newConversation()]; // 至少保留一个会话
      setActiveId((cur) => (next.some((c) => c.id === cur) ? cur : next[0].id));
      return next;
    });
    if (isGuest) return; // 游客：纯内存，本地即完成
    for (const id of ids) {
      try {
        await deleteConversation(id); // 服务端无记录（未发送过）→ 404 静默
      } catch {
        /* 与单删同款：失败静默不阻塞 */
      }
    }
    refreshList();
  }, [selectedIds, isGuest, refreshList]);

  return {
    convos, active, activeId, filteredConvos, searchText, setSearchText,
    renamingId, setRenamingId, renameText, setRenameText,
    selecting, setSelecting, selectedIds,
    refreshList, openConversation, updateConvo, patchLastAssistant,
    newChat, removeChat, commitRename, toggleSelect, selectAllVisible, exitSelect,
    doBatchRemove,
  };
}
