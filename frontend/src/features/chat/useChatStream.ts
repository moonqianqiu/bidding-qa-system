"use client";

// 问答流 hook（A8 中断保留 / A4 seq 守卫 / WP3 rAF 合帧）：send/stop 与流式渲染机器。
// 从 page.tsx 抽出（C7）：会话状态经依赖注入（patchLastAssistant/refreshList 等），
// 本 hook 只管「一次提问的生命周期」——请求序号防旧流覆盖、token/thinking 入缓冲
// 合帧 flush（~30fps）、done/error/中断收尾。

import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatMessage } from "@/lib/types";
import { makeSeqGuard, streamChat } from "@/lib/sse";
import type { AuthUser } from "@/lib/auth";
import { compressImage } from "@/lib/image";
import { postVision } from "@/lib/api";
import type { useChatSessions } from "@/features/chat/useChatSessions";

type Sessions = ReturnType<typeof useChatSessions>;

export interface Attachment {
  name: string;
  description: string;
  ref: string;
  error?: boolean;
}

export function useChatStream(
  authUser: AuthUser | null,
  sessions: Sessions,
  webEnabled: boolean,
  thinkingEnabled: boolean,
  provider: string,
  lang: "zh" | "en",
) {
  const { active, updateConvo, patchLastAssistant, refreshList } = sessions;
  const [streaming, setStreaming] = useState(false);
  const [attachment, setAttachment] = useState<Attachment | null>(null);
  const [uploadingImage, setUploadingImage] = useState(false);

  const abortRef = useRef<AbortController | null>(null);
  const requestSeqRef = useRef(0); // A8 请求序号：旧流事件不得覆盖新会话
  // WP3 打字机平滑：token/thinking 先入缓冲，rAF 合帧 flush（~30fps）。
  // 量测依据（2026-11-08）：后端 token 到达 p50=75ms 相对平滑，卡顿主因是每 token
  // 全量渲染；用户体系后会话改服务端主，localStorage 落盘逻辑整体移除。
  const pendingRef = useRef({ content: "", thinking: "" });
  const rafRef = useRef<number | null>(null);
  const lastFlushRef = useRef(0);

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

  // A3 图片上传：压缩 → /api/vision 换引用（游客禁传：入口已隐藏，此处双保险）
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
        setAttachment({ name: file.name, description: "", ref: "", error: true }); // 5031/413/网络，统一错误 chip
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
          onEvent: (event) => {
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
    [active, streaming, webEnabled, thinkingEnabled, provider, lang, attachment,
     updateConvo, patchLastAssistant, flushPending, startFlusher, stopFlusher, refreshList],
  );

  const stop = useCallback(() => {
    abortRef.current?.abort(); // 中断保留已流内容（A8）
  }, []);

  return { streaming, attachment, setAttachment, uploadingImage, pickFile, send, stop };
}
