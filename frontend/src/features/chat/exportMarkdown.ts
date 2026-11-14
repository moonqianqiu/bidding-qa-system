"use client";

// 导出 Markdown（WP4）：当前会话 → .md 下载（标题 + 问答分段 + 来源清单）。纯函数。

import type { Conversation } from "@/lib/types";

type Labels = { exportQ: string; exportA: string; sourcesCount: (n: number) => string };

export function exportChatMarkdown(active: Conversation, t: Labels) {
  const lines: string[] = [`# ${active.title}`, ""];
  for (const m of active.messages) {
    lines.push(m.role === "user" ? `## ${t.exportQ}` : `## ${t.exportA}`, "", m.content, "");
    if (m.sources?.length) {
      lines.push(`**${t.sourcesCount(m.sources.length)}**`);
      lines.push(...m.sources.map((s, i) =>
        `${i + 1}. ${s.title}${s.url ? ` (${s.url})` : ""}`), "");
    }
  }
  const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${active.title || "conversation"}.md`;
  a.click();
  URL.revokeObjectURL(url);
}
