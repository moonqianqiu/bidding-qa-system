// B2 SSE-over-fetch 解析（POST 不能用 EventSource；docs/01 §3.4 契约前端对齐）
// seq 消费排查乱序；ping 忽略（可插入任意间隙、不计业务序）；reset 清空已流正文。

import { clearAuth, getToken } from "./auth";
import type { SSEEvent } from "./types";

// SSE 直连后端端口（docs/01 §1：生产经 /api/* 代理，SSE 直连——dev 代理
// 会把整个 SSE 缓冲到响应结束，实测事件全部压到最后 1ms 批量到达）。
// 覆盖手段：构建时设 VITE_API_BASE=""（走同源代理）或指向其它后端地址。
const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000";

// 用户体系：SSE 与 REST 同一凭证（Authorization 头贯通）；401/403+4031 清态跳登录。
function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function kickIfAuthFailed(status: number, code: number) {
  if ((status === 401 || (status === 403 && code === 4031))
      && !location.pathname.startsWith("/login")) {
    clearAuth();
    window.location.assign("/login");
  }
}

export type StreamHandlers = {
  onEvent: (event: SSEEvent) => void;
  onError: (err: Error) => void;
};

export async function streamChat(
  payload: {
    conversation_id: string | null;
    message: string;
    web_enabled: boolean;
    thinking_enabled: boolean;
    image_refs?: string[]; // A3：/api/vision 换得的引用，服务端按 ref 注入图片描述
    provider?: string | null; // A12/C8 档位路由（空 = 默认槽位）
    client_msg_id: string;
    lang?: "zh" | "en"; // P1：界面语言 → 回答语言（缺省 zh，后端 Literal 契约）
  },
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}/api/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(payload),
      signal,
    });
  } catch (err) {
    if ((err as Error).name === "AbortError") return; // 主动中断不是错误
    handlers.onError(new Error("无法连接后端服务（:8000 未启动？）"));
    return;
  }
  if (!resp.ok || !resp.body) {
    let code = 0;
    let message = `HTTP ${resp.status}`;
    try {
      const body = (await resp.json()) as { error_code?: number; message?: string };
      code = body.error_code ?? 0;
      message = body.message ?? message;
    } catch {
      /* 非 JSON 错误体 */
    }
    kickIfAuthFailed(resp.status, code);
    handlers.onError(new Error(`[${code || resp.status}] ${message}`));
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const event = parseBlock(block);
        if (event) handlers.onEvent(event);
      }
    }
  } catch (err) {
    if ((err as Error).name !== "AbortError") handlers.onError(err as Error);
  }
}

export function parseBlock(block: string): SSEEvent | null {
  for (const line of block.split("\n")) {
    if (line.startsWith("data:")) {
      try {
        return JSON.parse(line.slice(5).trim()) as SSEEvent;
      } catch {
        return null;
      }
    }
  }
  return null;
}

/** seq 消费器：乱序/重复丢弃（ping 不计业务序，永远放行）。 */
export function makeSeqGuard() {
  let last = 0;
  return (event: SSEEvent): boolean => {
    if (event.type === "ping") return true;
    if (typeof event.seq === "number" && event.seq <= last) return false;
    if (typeof event.seq === "number") last = event.seq;
    return true;
  };
}
