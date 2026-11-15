// REST 封装（docs/01 §3.1）：health/auth/conversations/feedback/vision/providers。
// 用户体系：全部请求自动带 Authorization（localStorage bidqa.token）；
// 401 / 403+4031（游客被踢）→ 清态跳 /login（登录页自身除外，凭据错误要就地展示）。

import { clearAuth, getToken, type Role } from "./auth";
import type { SSEEvent } from "./types";

export class ApiError extends Error {
  status: number;
  code: number; // 后端 error_code（docs/01 §3.2），网络层错误为 0
  constructor(message: string, status: number, code: number) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export async function jfetch<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const resp = await fetch(url, { ...init, headers });
  if (!resp.ok) {
    const body = (await resp.json().catch(() => ({}))) as {
      message?: string;
      error_code?: number;
    };
    const kicked =
      resp.status === 401 || (resp.status === 403 && body.error_code === 4031);
    if (kicked && !location.pathname.startsWith("/login")) {
      clearAuth();
      window.location.assign("/login");
    }
    throw new ApiError(body.message ?? `HTTP ${resp.status}`, resp.status,
      body.error_code ?? 0);
  }
  return (await resp.json()) as T;
}

export const getHealth = () => jfetch<Health>("/api/health");

export type Health = {
  status: string;
  capabilities: Record<string, boolean>;
  components: Record<string, { status: string; latency_ms: number }>;
};

// ── 用户体系（docs/01 §3.1）──
export type MeResponse = {
  authenticated: boolean;
  users_enabled: boolean;
  guest_enabled: boolean;
  username?: string;
  role?: Role;
};

export const getMe = () => jfetch<MeResponse>("/api/auth/me");

export type SessionResponse = { token: string; username: string; role: Role };

export const login = (username: string, password: string) =>
  jfetch<SessionResponse>("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });

export const loginGuest = () =>
  jfetch<SessionResponse>("/api/auth/guest", { method: "POST" });

export const logout = () =>
  jfetch<{ logged_out: boolean }>("/api/auth/logout", { method: "POST" });

export type Providers = {
  default_model: string;
  profiles: { name: string; model: string }[];
};

// A12：档位列表（后端只回名字与模型，无 key）
export const getProviders = () => jfetch<Providers>("/api/system/providers");

export type ConvSummary = {
  id: string;
  title: string;
  provider: string;
  created_at: string;
  updated_at: string;
};

export const listConversations = () =>
  jfetch<{ conversations: ConvSummary[] }>("/api/conversations");

export type ConvDetail = { id: string; title: string; messages: { items: StoredMsg[] } };

// 落库 item（后端 conversations.messages.items）：v2 起 assistant item 附 done 收尾
// 结构 + ts（刷新后回放来源/工具/图表/时间）；v1 旧行仅 role/content（正文回放）。
export type StoredMsg = {
  role: "user" | "assistant";
  content: string;
  ts?: number;
  done?: Partial<SSEEvent>;
};

export const getConversation = (id: string) =>
  jfetch<ConvDetail>(`/api/conversations/${id}`);

export const deleteConversation = (id: string) =>
  jfetch<{ deleted: string }>(`/api/conversations/${id}`, { method: "DELETE" });

// WP4 会话重命名（后端 PATCH，非空标题）
export const renameConversation = (id: string, title: string) =>
  jfetch<{ id: string; title: string }>(`/api/conversations/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });

export type FeedbackBody = {
  conversation_id: string | null;
  question: string;
  rating: "up" | "down";
  sources: { id: string; title: string }[];
  trace_id: string;
  sources_belongs_to_round: boolean;
  answer?: string; // G4 点踩必评（忠实度判分）
};

export const postFeedback = (body: FeedbackBody) =>
  jfetch<{ id: number; rating: string }>("/api/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

// 图片解析 → image_ref（docs/01 §3.1：聊天只传引用不传 base64）
// v2.3 双通道：original=高分辨率原件（OCR 用），mode=auto(Qwen 路由)/ocr(手动按文档)/describe
export type VisionResult = {
  image_ref: string;
  description: string;
  filename: string | null;
  mode_used?: "describe" | "ocr";
  route_by?: "auto" | "manual" | "unavailable";
  degraded?: boolean;
};

export const postVision = (
  image: string,
  filename: string,
  opts?: { original?: string; mode?: "auto" | "ocr" | "describe" },
) =>
  jfetch<VisionResult>("/api/vision", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ image, filename, ...opts }),
  });

// ── 控制台（A15/B11/B12；全部 admin-only，403 → /console 页内提示态）──
export type ConsoleStats = {
  pg:
    | { bidding_procurement: number; conversations: number; feedback: number; users: number }
    | { status: "不可用" };
  qdrant: { vectors: number; collection: string } | { status: "不可用" };
  neo4j:
    | { labels: Record<string, number>; total: number }
    | { status: "不可用" };
};

export const getConsoleStats = () => jfetch<ConsoleStats>("/api/system/console");

// 运行时快照（KPI uptime/请求/错误 + 运行时区块）
export type Metrics = {
  uptime_s: number;
  requests: Record<string, { count: number; errors: number }>;
  log_buffer_size: number;
  resources: Record<string, number | string>;
  online_eval: { enabled: boolean; sample_rate: number } & Record<string, unknown>;
};

export const getMetrics = () => jfetch<Metrics>("/api/system/metrics");

// 内存日志环（控制台日志区块 5s 轮询）
export type LogEntry = { ts: string; level: string; name: string; message: string };

export const getLogs = (level = "INFO", q = "") => {
  const params = new URLSearchParams({ level, q });
  return jfetch<{ logs: LogEntry[]; total: number; buffered: number }>(
    `/api/system/logs?${params.toString()}`,
  );
};

export type UserRow = {
  username: string;
  role: "admin" | "user";
  disabled: boolean;
  created_at: string;
  sessions: number;
};

export const getUsers = () =>
  jfetch<{ users: UserRow[] }>("/api/system/users");

export const createUser = (username: string, password: string, role: "admin" | "user") =>
  jfetch<{ created: boolean; id: string; username: string; role: string }>(
    "/api/system/users",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, role }),
    },
  );

export const resetPassword = (username: string, password: string) =>
  jfetch<{ reset: boolean; sessions_revoked: number }>(
    `/api/system/users/${encodeURIComponent(username)}/reset-password`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    },
  );

export const setUserDisabled = (username: string, disabled: boolean) =>
  jfetch<{ disabled: boolean; username: string }>(
    `/api/system/users/${encodeURIComponent(username)}/${disabled ? "disable" : "enable"}`,
    { method: "POST" },
  );

export const setGuestEnabled = (enabled: boolean) =>
  jfetch<{ guest_enabled: boolean }>("/api/system/settings/guest-enabled", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });

export type AdminConvRow = {
  id: string;
  title: string;
  provider: string;
  updated_at: string;
  user_id: string | null;
  owner: string | null;
  items: StoredMsg[];
  has_reply: boolean;
};

export const listAllConversations = (limit = 200, q = "") => {
  const params = new URLSearchParams({ limit: String(limit) });
  if (q) params.set("q", q);
  return jfetch<{ conversations: AdminConvRow[] }>(
    `/api/system/conversations?${params.toString()}`,
  );
};

export const adminDeleteConversation = (id: string) =>
  jfetch<{ deleted: boolean; id: string }>(`/api/system/conversations/${id}`, {
    method: "DELETE",
  });

export const purgeEmptyConversations = (dryRun: boolean) =>
  jfetch<{ dry_run: boolean; count?: number; samples?: string[]; deleted?: number }>(
    "/api/system/conversations/purge-empty",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: dryRun }),
    },
  );
