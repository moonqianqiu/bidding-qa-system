"use client";

// 控制台数据 hook（A15）：console 聚合 + health + metrics + 日志轮询。
// 轮询节奏：核心 30s（后端同款缓存）/ 日志 5s；403（非 admin）→ 页内提示态
// （门禁在服务端，藏入口只是体验）；轮询失败静默（下个周期再试）。

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError, getConsoleStats, getHealth, getLogs, getMe, getMetrics,
  type ConsoleStats, type Health, type LogEntry, type Metrics,
} from "@/lib/api";
import { getUser } from "@/lib/auth";

export function useConsoleData() {
  const [stats, setStats] = useState<ConsoleStats | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [buffered, setBuffered] = useState(0);
  const [guestOn, setGuestOn] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  // A11 日志过滤（审计：level/q 参数 API 早就有、UI 从未接）——ref 供 5s 轮询读现值
  const [logLevel, setLogLevel] = useState("INFO");
  const [logQuery, setLogQuery] = useState("");
  const logFilterRef = useRef({ level: "INFO", q: "" });
  logFilterRef.current = { level: logLevel, q: logQuery.trim() };

  const refreshCore = useCallback(async () => {
    const me = getUser();
    if (me && me.role !== "admin") {
      setForbidden(true); // 本地身份已明示非管理员：不发请求直接进提示态
      setLoaded(true);
      return;
    }
    try {
      const [st, h, m] = await Promise.all([getConsoleStats(), getHealth(), getMetrics()]);
      setStats(st);
      setHealth(h);
      setMetrics(m);
      setError("");
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) setForbidden(true);
      else setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoaded(true);
    }
  }, []);

  const refreshLogs = useCallback(async () => {
    try {
      const { level, q } = logFilterRef.current;
      const r = await getLogs(level, q);
      setLogs(r.logs.slice(-50).reverse());
      setBuffered(r.buffered);
    } catch {
      /* 轮询失败静默（下个周期再试） */
    }
  }, []);

  useEffect(() => {
    refreshCore();
    refreshLogs();
    getMe()
      .then((me) => setGuestOn(me.guest_enabled))
      .catch(() => {});
    const core = setInterval(refreshCore, 30_000);
    const lg = setInterval(refreshLogs, 5_000);
    return () => {
      clearInterval(core);
      clearInterval(lg);
    };
  }, [refreshCore, refreshLogs]);

  return {
    stats, health, metrics, logs, buffered, guestOn, setGuestOn,
    forbidden, error, loaded, refreshCore, refreshLogs,
    logLevel, setLogLevel, logQuery, setLogQuery, // A11 日志过滤
  };
}
