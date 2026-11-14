"use client";

// 消息流面板：空态 hero + 消息列表 + 自动滚底——纯展示件。

import { useEffect, useRef } from "react";
import type { ChatMessage } from "@/lib/types";
import { useI18n } from "@/lib/i18n";
import MessageItem from "@/components/MessageItem";

interface Props {
  messages: ChatMessage[];
  streaming: boolean;
  onFeedback: (m: ChatMessage, rating: "up" | "down") => void;
}

export default function ChatPanel({ messages, streaming, onFeedback }: Props) {
  const { t } = useI18n();
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length, streaming]);

  return (
    <div className="flex-1 overflow-y-auto px-4 py-6">
      <div className="mx-auto max-w-3xl space-y-6">
        {!messages.length && (
          <div className="mt-24 text-center">
            <h2 className="text-2xl font-semibold text-fg">{t.heroTitle}</h2>
            <p className="mt-2 text-sm text-faint">{t.heroSub}</p>
          </div>
        )}
        {messages.map((m) => (
          <MessageItem key={m.id} message={m} onFeedback={onFeedback} />
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
