"use client";

// A5 来源卡片：来源条目 + 相关度条 + 外链安全（target=_blank + rel）；文案随语言设置。

import type { SourceItem } from "@/lib/types";
import { useI18n } from "@/lib/i18n";

function scorePercent(score: number | null | undefined): number | null {
  if (score === null || score === undefined) return null;
  const pct = Math.round(score * 100);
  return Math.max(0, Math.min(100, pct));
}

export default function SourceCard({ source, index }: { source: SourceItem; index: number }) {
  const { t } = useI18n();
  const pct = scorePercent(source.score);
  const isWeb = source.source_type === "web";
  return (
    <div className="rounded-lg border border-line bg-panel/60 p-3 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 min-w-0">
          <span className="inline-flex h-4.5 w-4.5 shrink-0 items-center justify-center rounded bg-sky-100 text-[10px] font-semibold text-sky-700 dark:bg-sky-900 dark:text-sky-300">
            {index + 1}
          </span>
          <span className="rounded bg-line px-1.5 py-0.5 text-[10px] text-dim">
            {source.source_type || t.sourceFallback}
          </span>
          <span className="truncate font-medium text-fg" title={source.title}>
            {source.title || t.untitledSource}
          </span>
        </span>
        {pct !== null && (
          <span className="shrink-0 text-[10px] text-dim">
            {t.relevance} {pct}%
          </span>
        )}
      </div>
      {pct !== null && (
        <div className="mt-1.5 h-1 w-full overflow-hidden rounded bg-line">
          <div
            className="h-full rounded bg-gradient-to-r from-sky-600 to-cyan-400"
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
      <p className="mt-1.5 line-clamp-3 whitespace-pre-wrap text-dim">{source.text}</p>
      <div className="mt-1 flex items-center gap-2 text-[10px] text-faint">
        {source.ref && <span>{t.clause} {source.ref}</span>}
        {source.page != null && <span>{t.page(source.page)}</span>}
        {source.meta?.award_amount != null && (
          <span>
            {t.amount} {Number(source.meta.award_amount).toLocaleString()} {t.amountUnit}
          </span>
        )}
        {source.url && (
          <a
            href={source.url}
            target="_blank"
            rel="noopener noreferrer nofollow"
            className="ml-auto text-sky-600 hover:text-sky-500 dark:text-sky-500 dark:hover:text-sky-400"
          >
            {t.openLink}
          </a>
        )}
        {isWeb && <span className="ml-auto text-faint">{t.webResult}</span>}
      </div>
    </div>
  );
}
