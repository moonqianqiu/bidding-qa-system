"use client";

// A15 控制台（KPI 仪表盘布局，docs/02）：KPI 行 7 卡 + 双栏（服务健康｜运行时）
// +（数据规模｜日志 5s 轮询）+ 管理员动作区 + 深链行。
// 数据：/api/system/console（30s 后端缓存）+ /api/health + /api/system/metrics；
// 30s 自动刷新 + 手动 ↻；403（非 admin）→ 页内提示态（门禁在服务端，藏入口只是体验）。
// 全语义 token（bg-base/bg-panel/border-line/text-fg…）——不硬编码 zinc（eval 页反例）。

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  getConsoleStats,
  getHealth,
  getLogs,
  getMe,
  getMetrics,
  type ConsoleStats,
  type Health,
  type LogEntry,
  type Metrics,
} from "@/lib/api";
import { getUser } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";
import ConversationAdmin from "@/components/console/ConversationAdmin";
import GuestToggle from "@/components/console/GuestToggle";
import UserAdmin from "@/components/console/UserAdmin";

/** 「不可用」节剥离：{status:"不可用"} → null，可用 → 数据形状（KPI/规模区共用）。 */
type PgStats = Extract<ConsoleStats["pg"], { bidding_procurement: number }>;
type QdStats = Extract<ConsoleStats["qdrant"], { vectors: number }>;
type NeoStats = Extract<ConsoleStats["neo4j"], { total: number }>;

function availPg(v: ConsoleStats["pg"] | undefined): PgStats | null {
  return v && !("status" in v) ? v : null;
}

function availQd(v: ConsoleStats["qdrant"] | undefined): QdStats | null {
  return v && !("status" in v) ? v : null;
}

function availNeo(v: ConsoleStats["neo4j"] | undefined): NeoStats | null {
  return v && !("status" in v) ? v : null;
}

function fmtUptime(s: number): string {
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function num(v: number | undefined | null): string {
  return v === undefined || v === null ? "-" : v.toLocaleString();
}

export default function ConsolePage() {
  const { t } = useI18n();
  const [stats, setStats] = useState<ConsoleStats | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [buffered, setBuffered] = useState(0);
  const [guestOn, setGuestOn] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);

  const refreshCore = useCallback(async () => {
    const me = getUser();
    if (me && me.role !== "admin") {
      setForbidden(true); // 本地身份已明示非管理员：不发请求直接进提示态
      setLoaded(true);
      return;
    }
    try {
      const [st, h, m] = await Promise.all([
        getConsoleStats(),
        getHealth(),
        getMetrics(),
      ]);
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
      const r = await getLogs("INFO");
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

  const requests = Object.values(metrics?.requests ?? {}).reduce(
    (acc, r) => ({ count: acc.count + r.count, errors: acc.errors + r.errors }),
    { count: 0, errors: 0 },
  );

  const pg = availPg(stats?.pg);
  const qd = availQd(stats?.qdrant);
  const neo = availNeo(stats?.neo4j);
  const svcOk = health?.status === "ok";

  const kpi: { label: string; value: string; tone?: "ok" | "warn" }[] = [
    {
      label: t.kpiStatus,
      value: health ? (svcOk ? t.healthOk.replace("● ", "") : "degraded") : t.na,
      tone: health ? (svcOk ? "ok" : "warn") : undefined,
    },
    { label: t.kpiUptime, value: metrics ? fmtUptime(metrics.uptime_s) : t.na },
    { label: t.kpiRequests, value: metrics ? num(requests.count) : t.na },
    {
      label: t.kpiErrors,
      value: metrics ? num(requests.errors) : t.na,
      tone: requests.errors > 0 ? "warn" : undefined,
    },
    { label: t.kpiPgRows, value: stats ? (pg ? num(pg.bidding_procurement) : t.na) : "…" },
    { label: t.kpiVectors, value: stats ? (qd ? num(qd.vectors) : t.na) : "…" },
    { label: t.kpiGraphNodes, value: stats ? (neo ? num(neo.total) : t.na) : "…" },
  ];

  const cardHead = "text-sm font-semibold text-fg";

  if (forbidden) {
    return (
      <main className="flex min-h-screen items-center justify-center px-4">
        <div className="max-w-sm rounded-2xl border border-line bg-panel p-6 text-center">
          <p className="text-sm text-fg">⚠ {t.consoleForbidden}</p>
          <a href="/" className="mt-4 inline-block text-xs text-dim hover:text-fg">
            {t.consoleBack}
          </a>
        </div>
      </main>
    );
  }

  return (
    <main className="mx-auto min-h-screen max-w-6xl px-4 py-6">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-fg">▦ {t.consoleTitle}</h1>
        <div className="flex items-center gap-3 text-xs">
          <a href="/" className="text-dim hover:text-fg">{t.consoleBack}</a>
          <a href="/eval" className="text-dim hover:text-fg">{t.consoleEvalLink}</a>
          <button
            onClick={() => {
              refreshCore();
              refreshLogs();
            }}
            className="rounded-lg border border-line px-2 py-1 text-dim hover:text-fg"
            title={t.consoleRefresh}
          >
            ↻ {t.consoleRefresh}
          </button>
        </div>
      </div>
      {error && <p className="mt-2 text-xs text-red-600 dark:text-red-300">{error}</p>}

      {/* KPI 行（7 卡） */}
      <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7">
        {kpi.map((c) => (
          <div key={c.label} className="rounded-xl border border-line bg-panel p-3">
            <p className="text-[10px] text-faint">{c.label}</p>
            <p
              className={`mt-1 truncate text-base font-semibold ${
                c.tone === "ok"
                  ? "text-emerald-500 dark:text-emerald-300"
                  : c.tone === "warn"
                    ? "text-amber-500 dark:text-amber-300"
                    : "text-fg"
              }`}
              title={c.value}
            >
              {c.value}
            </p>
          </div>
        ))}
      </div>

      {/* 双栏 ①服务健康｜②运行时 */}
      <div className="mt-4 grid gap-3 lg:grid-cols-2">
        <section className="rounded-xl border border-line bg-panel p-4">
          <h3 className={cardHead}>{t.secHealth}</h3>
          <ul className="mt-2 space-y-1 text-xs">
            {Object.entries(health?.components ?? {}).map(([name, comp]) => (
              <li key={name} className="flex items-center justify-between">
                <span className="text-dim">{name}</span>
                <span
                  className={
                    comp.status === "ok"
                      ? "text-emerald-500 dark:text-emerald-300"
                      : "text-amber-500 dark:text-amber-300"
                  }
                >
                  {comp.status === "ok" ? `● ${comp.latency_ms}ms` : `⚠ ${comp.status}`}
                </span>
              </li>
            ))}
            {health === null && <li className="text-faint">…</li>}
          </ul>
        </section>
        <section className="rounded-xl border border-line bg-panel p-4">
          <h3 className={cardHead}>{t.secRuntime}</h3>
          <ul className="mt-2 space-y-1 text-xs">
            {Object.entries(metrics?.resources ?? {}).map(([k, v]) => (
              <li key={k} className="flex items-center justify-between">
                <span className="text-dim">{k}</span>
                <span className="text-fg">{String(v)}</span>
              </li>
            ))}
            {metrics?.online_eval && (
              <li className="flex items-center justify-between">
                <span className="text-dim">online_eval</span>
                <span className="text-fg">
                  {metrics.online_eval.enabled
                    ? `on @${metrics.online_eval.sample_rate}`
                    : "off"}
                </span>
              </li>
            )}
            {metrics?.log_buffer_size !== undefined && (
              <li className="flex items-center justify-between">
                <span className="text-dim">log_buffer</span>
                <span className="text-fg">{t.logBuffered(metrics.log_buffer_size)}</span>
              </li>
            )}
            {metrics === null && <li className="text-faint">…</li>}
          </ul>
        </section>
      </div>

      {/* 双栏 ③数据规模｜④日志 */}
      <div className="mt-3 grid gap-3 lg:grid-cols-2">
        <section className="rounded-xl border border-line bg-panel p-4">
          <h3 className={cardHead}>{t.secData}</h3>
          {!stats && <p className="mt-2 text-xs text-faint">…</p>}
          {stats?.pg && (
            <div className="mt-2 text-xs">
              <p className="text-faint">postgres</p>
              <ul className="mt-1 space-y-1">
                {!pg ? (
                  <li className="text-amber-500 dark:text-amber-300">⚠ {t.na}</li>
                ) : (
                  <>
                    {(
                      [
                        ["bidding_procurement", pg.bidding_procurement],
                        ["conversations", pg.conversations],
                        ["feedback", pg.feedback],
                        ["users", pg.users],
                      ] as const
                    ).map(([k, v]) => (
                      <li key={k} className="flex justify-between">
                        <span className="text-dim">{k}</span>
                        <span className="text-fg">{num(v)}</span>
                      </li>
                    ))}
                  </>
                )}
              </ul>
            </div>
          )}
          {stats?.qdrant && (
            <div className="mt-3 text-xs">
              <p className="text-faint">
                qdrant{qd ? ` · ${qd.collection}` : ""}
              </p>
              <p className="mt-1 text-fg">
                {qd ? `vectors ${num(qd.vectors)}` : `⚠ ${t.na}`}
              </p>
            </div>
          )}
          {stats?.neo4j && (
            <div className="mt-3 text-xs">
              <p className="text-faint">neo4j</p>
              {!neo ? (
                <p className="mt-1 text-amber-500 dark:text-amber-300">⚠ {t.na}</p>
              ) : (
                <ul className="mt-1 grid grid-cols-2 gap-x-4 gap-y-1">
                  {Object.entries(neo.labels).map(([label, n]) => (
                    <li key={label} className="flex justify-between">
                      <span className="text-dim">{label}</span>
                      <span className="text-fg">{num(n)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </section>
        <section className="rounded-xl border border-line bg-panel p-4">
          <div className="flex items-center justify-between">
            <h3 className={cardHead}>{t.secLogs}</h3>
            <span className="text-[10px] text-faint">5s · {t.logBuffered(buffered)}</span>
          </div>
          <div className="mt-2 max-h-64 overflow-y-auto rounded-lg border border-line bg-base p-2">
            {logs.length === 0 ? (
              <p className="py-3 text-center text-xs text-faint">{t.noLogs}</p>
            ) : (
              <ul className="space-y-0.5 font-mono text-[11px] leading-4">
                {logs.map((l, i) => (
                  <li key={i} className="truncate">
                    <span className="text-faint">{l.ts}</span>{" "}
                    <span
                      className={
                        l.level === "ERROR"
                          ? "text-red-500 dark:text-red-300"
                          : l.level === "WARNING"
                            ? "text-amber-500 dark:text-amber-300"
                            : "text-dim"
                      }
                    >
                      {l.level}
                    </span>{" "}
                    <span className="text-fg" title={l.message}>{l.message}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </section>
      </div>

      {/* ⑤管理员动作区 */}
      <div className="mt-3">
        <h3 className={cardHead}>{t.secActions}</h3>
        <div className="mt-2 grid gap-3 lg:grid-cols-2">
          <UserAdmin />
          <GuestToggle enabled={guestOn} onChange={setGuestOn} />
        </div>
        <div className="mt-3">
          <ConversationAdmin />
        </div>
      </div>

      {/* ⑥深链行 */}
      <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-line pt-3 text-xs text-faint">
        <span>{t.consoleDeepLinks}:</span>
        <a
          href="http://127.0.0.1:6333/dashboard"
          target="_blank"
          rel="noreferrer"
          className="text-dim hover:text-fg"
        >
          Qdrant ↗
        </a>
        <a
          href="http://127.0.0.1:7474"
          target="_blank"
          rel="noreferrer"
          className="text-dim hover:text-fg"
        >
          Neo4j browser ↗
        </a>
        <span className="ml-auto">{loaded ? "" : "…"}</span>
      </div>
    </main>
  );
}
