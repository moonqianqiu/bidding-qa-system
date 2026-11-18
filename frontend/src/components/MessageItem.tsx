"use client";

// 消息渲染：Markdown+表格 / thinking 折叠 / 状态条 / 来源与图表 / 反馈（A7）/ 错误与中断附注。

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Components } from "react-markdown";
import { useState } from "react";
import type { ChatMessage } from "@/lib/types";
import { stageText, useI18n } from "@/lib/i18n";
import SourceCard from "./SourceCard";
import ChartBlock from "./ChartBlock";

const markdownClass: Components = {
  p: (props) => <p className="my-2 leading-7 first:mt-0 last:mb-0" {...props} />,
  ul: (props) => <ul className="my-2 list-disc space-y-1 pl-5" {...props} />,
  ol: (props) => <ol className="my-2 list-decimal space-y-1 pl-5" {...props} />,
  li: (props) => <li className="leading-6" {...props} />,
  h1: (props) => <h1 className="my-3 text-lg font-semibold" {...props} />,
  h2: (props) => <h2 className="my-3 text-base font-semibold" {...props} />,
  h3: (props) => <h3 className="my-2 text-sm font-semibold" {...props} />,
  table: (props) => (
    <div className="my-3 overflow-x-auto rounded-lg border border-line">
      <table className="w-full text-xs" {...props} />
    </div>
  ),
  th: (props) => (
    <th className="border-b border-line bg-panel px-3 py-2 text-left font-medium text-fg" {...props} />
  ),
  td: (props) => <td className="border-b border-line/60 px-3 py-2 text-dim" {...props} />,
  code: (props) => (
    <code
      className="rounded bg-line px-1 py-0.5 font-mono text-[0.85em] text-amber-700 dark:text-amber-300"
      {...props}
    />
  ),
  a: (props) => (
    <a
      className="text-sky-600 hover:underline dark:text-sky-400"
      target="_blank"
      rel="noopener noreferrer nofollow"
      {...props}
    />
  ),
  blockquote: (props) => (
    <blockquote className="my-2 border-l-2 border-line pl-3 text-dim" {...props} />
  ),
};

/** 时间戳（WP4）：ts 存了没渲染过，现在 HH:mm 展示。 */
function fmtTime(ts: number): string {
  try {
    return new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch {
    return "";
  }
}

export default function MessageItem({
  message,
  onFeedback,
}: {
  message: ChatMessage;
  onFeedback?: (message: ChatMessage, rating: "up" | "down") => void;
}) {
  const [thinkingOpen, setThinkingOpen] = useState(false);
  const [copied, setCopied] = useState(false); // WP4 复制回答：✓ 显示 1.5s 后还原
  const { t } = useI18n();
  const isUser = message.role === "user";

  // WP5b（schema_v=2）：Plan/Reflection 轨迹——执行计划折叠列表 + 已补查徽标。
  // plan_steps 恒在位（空=未规划）；Array.isArray 守卫旧快照/异常结构。
  const done = message.done;
  const planSteps = Array.isArray(done?.plan_steps) ? done!.plan_steps! : [];

  const copyAnswer = async () => {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* 剪贴板不可用（非安全上下文等）静默 */
    }
  };

  if (isUser) {
    return (
      <div className="flex flex-col items-end gap-0.5">
        <div className="max-w-[85%] rounded-2xl rounded-br-md bg-accent px-4 py-2.5 text-sm leading-6 text-white">
          {message.content}
        </div>
        <span className="text-[10px] text-faint">{fmtTime(message.ts)}</span>
      </div>
    );
  }

  const streaming = !done && !message.error && !message.interrupted;

  return (
    <div className="flex flex-col gap-2">
      {message.thinking && (
        <div className="rounded-lg border border-line/80 bg-panel/40 text-xs">
          <button
            className="flex w-full items-center gap-1.5 px-3 py-2 text-left text-dim hover:text-fg"
            onClick={() => setThinkingOpen((v) => !v)}
          >
            <span className={`transition-transform ${thinkingOpen ? "rotate-90" : ""}`}>▸</span>
            {t.thinking}
          </button>
          {thinkingOpen && (
            <p className="max-h-60 overflow-y-auto whitespace-pre-wrap border-t border-line/60 px-3 py-2 text-dim">
              {message.thinking}
            </p>
          )}
        </div>
      )}

      {streaming && message.statusStage && (
        <div className="flex items-center gap-2 text-xs text-cyan-600 dark:text-cyan-400/90">
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-cyan-500 dark:bg-cyan-400" />
          {stageText(message.statusStage, t)}…
        </div>
      )}

      {planSteps.length > 0 && (
        <details className="group">
          <summary className="flex cursor-pointer list-none items-center gap-1 text-xs text-dim hover:text-fg [&::-webkit-details-marker]:hidden">
            <span className="transition-transform group-open:rotate-90">▸</span>
            {t.planTitle}
          </summary>
          <ol className="mt-1 list-decimal space-y-0.5 border-l-2 border-line/60 pl-5 text-xs leading-6 text-dim">
            {planSteps.map((step, i) => (
              <li key={i}>{step}</li>
            ))}
          </ol>
        </details>
      )}

      {message.content && (
        <div className="text-sm text-fg">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownClass}>
            {message.content}
          </ReactMarkdown>
        </div>
      )}

      {message.error && (
        <div className="rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-300">
          {t.errorPrefix}
          {message.error}
        </div>
      )}
      {message.interrupted && (
        <div className="text-xs text-amber-600 dark:text-amber-500/80">{t.interrupted}</div>
      )}

      {!!message.charts?.length && (
        <div className="space-y-2">
          {message.charts.map((chart) => (
            <ChartBlock key={chart.id || chart.title} chart={chart} />
          ))}
        </div>
      )}

      {!!message.sources?.length && (
        <details className="group">
          <summary className="flex cursor-pointer list-none items-center gap-1 text-xs text-dim hover:text-fg [&::-webkit-details-marker]:hidden">
            <span className="transition-transform group-open:rotate-90">▸</span>
            {t.sourcesCount(message.sources.length)}
            {done?.tool_name && !done.clarify && (
              <span className="ml-2 text-faint">
                {t.toolPrefix} {done.tool_name}
              </span>
            )}
            {done?.rounds != null && !done.clarify && (
              <span className="ml-2 text-faint">
                · {done.rounds} {t.rounds}
              </span>
            )}
            {done?.hit_cap && <span className="ml-2 text-amber-600">· {t.hitCap}</span>}
          </summary>
          <div className="mt-2 space-y-2">
            {message.sources.map((source, i) => (
              <SourceCard key={source.id || i} source={source} index={i} />
            ))}
          </div>
        </details>
      )}

      {done && !done.clarify && (
        <div className="flex items-center gap-2 text-xs text-faint">
          {onFeedback && (
            <>
              <button
                className={`rounded px-1.5 py-0.5 hover:bg-line ${
                  message.feedback === "up"
                    ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/60 dark:text-emerald-300"
                    : ""
                }`}
                title={t.helpful}
                onClick={() => onFeedback(message, "up")}
              >
                👍
              </button>
              <button
                className={`rounded px-1.5 py-0.5 hover:bg-line ${
                  message.feedback === "down"
                    ? "bg-red-100 text-red-700 dark:bg-red-900/60 dark:text-red-300"
                    : ""
                }`}
                title={t.unhelpful}
                onClick={() => onFeedback(message, "down")}
              >
                👎
              </button>
            </>
          )}
          {message.content && (
            <button
              className="rounded px-1.5 py-0.5 hover:bg-line"
              title={t.copy}
              onClick={copyAnswer}
            >
              {copied ? `✓ ${t.copied}` : "📋"}
            </button>
          )}
          <span className="text-faint">{fmtTime(message.ts)}</span>
          {done.reflected && (
            <span className="rounded border border-line px-1.5 py-0.5 text-dim">
              {t.reflectedBadge}
            </span>
          )}
          {done.elapsed_ms != null && (
            <span className="text-faint">{(done.elapsed_ms / 1000).toFixed(1)}s</span>
          )}
          {done.trace_id && <span className="text-faint">trace {done.trace_id}</span>}
        </div>
      )}
    </div>
  );
}
