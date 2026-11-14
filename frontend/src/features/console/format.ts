"use client";

// 控制台纯函数（A15）：「不可用」节剥离 + 展示格式化。
// 硬约束口径：{status:"不可用"} → null（KPI/规模区按 null 显示「…/na」），绝不记 0。

import type { ConsoleStats } from "@/lib/api";

export type PgStats = Extract<ConsoleStats["pg"], { bidding_procurement: number }>;
export type QdStats = Extract<ConsoleStats["qdrant"], { vectors: number }>;
export type NeoStats = Extract<ConsoleStats["neo4j"], { total: number }>;

export function availPg(v: ConsoleStats["pg"] | undefined): PgStats | null {
  return v && !("status" in v) ? v : null;
}

export function availQd(v: ConsoleStats["qdrant"] | undefined): QdStats | null {
  return v && !("status" in v) ? v : null;
}

export function availNeo(v: ConsoleStats["neo4j"] | undefined): NeoStats | null {
  return v && !("status" in v) ? v : null;
}

export function fmtUptime(s: number): string {
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`;
}

export function num(v: number | undefined | null): string {
  return v === undefined || v === null ? "-" : v.toLocaleString();
}
