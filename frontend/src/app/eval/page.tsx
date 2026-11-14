"use client";

// A10 评估页（M4-4 精简版）：离线快照列表（按命名规范=文件名排序）；评测门关闭时如实提示。

import { useEffect, useState } from "react";
import { getToken } from "@/lib/auth";

type Run = {
  name: string;
  kind: string;
  total: number | null;
  score: number | null;
  hit_cap_rate: number | null;
  link: string;
};

export default function EvalPage() {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [closed, setClosed] = useState(false);

  useEffect(() => {
    const token = getToken();
    fetch("/api/eval/runs", {
      headers: token ? { Authorization: `Bearer ${token}` } : undefined, // 用户体系：带会话凭证
    })
      .then(async (r) => {
        if (r.status === 404) {
          setClosed(true);
          return;
        }
        if (r.status === 401) {
          window.location.assign("/login"); // 未登录（用户体系开启）：跳登录而非误报评测门
          return;
        }
        const body = (await r.json()) as { runs: Run[] };
        setRuns(body.runs);
      })
      .catch(() => setClosed(true));
  }, []);

  return (
    <main className="mx-auto max-w-4xl p-8 text-zinc-200">
      <h1 className="text-lg font-semibold">评测快照</h1>
      {closed && (
        <p className="mt-4 rounded-lg border border-zinc-800 bg-zinc-900/60 p-4 text-sm text-zinc-400">
          评测门未开启（ONLINE_EVAL_ENABLED=false，§3.1 语义=整体 404）。离线评测通过 CLI 运行：
          <code className="mx-1 rounded bg-zinc-800 px-1.5 py-0.5 text-amber-300">
            uv run bidqa eval --golden eval/golden/agent_v0.jsonl --mode agent
          </code>
          快照落盘 eval/results/。
        </p>
      )}
      {runs && (
        <table className="mt-4 w-full text-xs">
          <thead>
            <tr className="border-b border-zinc-800 text-left text-zinc-500">
              <th className="py-2">快照</th>
              <th>类型</th>
              <th>用例</th>
              <th>分数</th>
              <th>hit_cap</th>
              <th>链路</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.name} className="border-b border-zinc-800/60">
                <td className="py-2 font-mono text-[11px] text-zinc-400">{run.name}</td>
                <td>{run.kind}</td>
                <td className="tabular-nums">{run.total ?? "—"}</td>
                <td className="tabular-nums">{run.score ?? "—"}</td>
                <td className="tabular-nums">
                  {run.hit_cap_rate != null && run.hit_cap_rate > 0.1 ? (
                    <span className="text-amber-500">{run.hit_cap_rate}</span>
                  ) : (
                    run.hit_cap_rate ?? "—"
                  )}
                </td>
                <td>{run.link}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <a href="/" className="mt-6 inline-block text-xs text-sky-500 hover:underline">
        ← 返回问答
      </a>
    </main>
  );
}
