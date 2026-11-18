// 领域类型（docs/01 §3.3/§3.4 契约的 TS 镜像）

export type SSEEvent = {
  seq: number;
  type: "status" | "token" | "thinking" | "reset" | "ping" | "done" | "error";
  // status
  stage?: string;
  // token/thinking
  text?: string;
  // reset
  reason?: string;
  // error
  error_code?: number;
  message?: string;
  // done 及以下字段
  sources?: SourceItem[];
  web_sources?: SourceItem[];
  charts?: ChartSpec[];
  tool_called?: boolean;
  tool_name?: string;
  tool_calls?: { name: string; arguments: Record<string, unknown> }[][];
  rounds?: number;
  hit_cap?: boolean;
  phase_times?: Record<string, number>;
  degraded?: string[];
  clarify?: boolean;
  // WP5b（schema_v=2）：Plan/Reflection 轨迹（空值不省略；澄清轮恒 []/false）
  plan_steps?: string[];
  reflected?: boolean;
  conversation_id?: string | null;
  provider?: string;
  trace_id?: string;
  elapsed_ms?: number;
  schema_v?: number;
};

export type SourceItem = {
  id: string;
  source_type: string;
  title: string;
  text: string;
  chapter?: string;
  ref?: string;
  page?: number | null;
  url?: string;
  score?: number | null;
  meta?: Record<string, unknown>;
};

export type ChartSpec = {
  id: string;
  kind: "bar" | "kpi";
  title: string;
  source?: string;
  format?: string;
  max?: number | null;
  items: { label: string; value: number }[];
  amounts?: { label: string; value: number | null }[];
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  thinking?: string;
  statusStage?: string; // 进行中的阶段提示
  events?: string[];    // 过程事件轨迹（状态条历史）
  sources?: SourceItem[];
  charts?: ChartSpec[];
  done?: Partial<SSEEvent>; // 收尾结构
  error?: string;
  interrupted?: boolean;    // A8 中断保留标记
  feedback?: "up" | "down" | null;
  ts: number;
};

export type Conversation = {
  id: string;
  title: string;
  updatedAt: number;
  messages: ChatMessage[];
};

// 图片附件（v2.3 双通道：选图只建档，发送时才经 /api/vision 换 ref；
// error=解析失败，chip 留错态、发送继续但不带图）
export type Attachment = {
  name: string;
  ref?: string;
  error?: boolean;
};
