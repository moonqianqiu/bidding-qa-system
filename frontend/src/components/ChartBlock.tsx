"use client";

// A6 图表渲染：bar（CSS 横向条，max 量程遵守）/ kpi 数字卡；无数据不出图（服务端已收口）。

import type { ChartSpec } from "@/lib/types";

function fmt(n: number, format?: string): string {
  if (format === "ratio") return `${Math.round(n * 100)}%`;
  if (format === "float") return n.toFixed(2);
  return Math.round(n).toLocaleString();
}

export default function ChartBlock({ chart }: { chart: ChartSpec }) {
  if (!chart.items?.length) return null; // 无数据不出图（A6）

  if (chart.kind === "kpi") {
    const item = chart.items[0];
    return (
      <div className="my-3 rounded-xl border border-line bg-panel/60 p-4">
        <p className="text-xs text-dim">{chart.title}</p>
        <p className="mt-1 text-3xl font-semibold tabular-nums text-sky-600 dark:text-cyan-300">
          {fmt(item.value, chart.format)}
        </p>
      </div>
    );
  }

  const max = chart.max ?? Math.max(...chart.items.map((i) => i.value), 1); // max 量程遵守
  return (
    <div className="my-3 rounded-xl border border-line bg-panel/60 p-4">
      <p className="text-xs font-medium text-dim">{chart.title}</p>
      <div className="mt-3 space-y-2">
        {chart.items.map((item) => {
          const pct = Math.max(2, Math.round((item.value / (max || 1)) * 100));
          return (
            <div key={item.label} className="flex items-center gap-2 text-xs">
              <span className="w-28 shrink-0 truncate text-right text-dim" title={item.label}>
                {item.label}
              </span>
              <div className="h-4 flex-1 overflow-hidden rounded bg-line/80">
                <div
                  className="flex h-full items-center justify-end rounded bg-gradient-to-r from-cyan-700 to-sky-500 pr-1.5"
                  style={{ width: `${pct}%` }}
                >
                  <span className="text-[10px] font-medium text-white/90">
                    {fmt(item.value, chart.format)}
                  </span>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
