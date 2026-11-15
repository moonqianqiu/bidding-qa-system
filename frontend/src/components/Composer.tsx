"use client";

// A3 输入区：IME 组词防误发 + 联网/深度思考/按文档解析开关 + 示例问题点击直发。
// 深浅色/语言收进设置对话框，入口在侧栏左下角（用户指定布局，不放输入区）。
// 图片上传（v2.3 双通道）：选图只建档、发送时才解析（useImageAttachment）；
// 「+」仅 vision 就绪时显示（capabilities.vision），按文档解析开关仅 OCR 就绪时显示。
// 输入条形态（2026-11-15 UI 轮）：默认一行扁平，多行自动长高、封顶 max（不超 128px）；
// 长高后右侧「+/发送」由横排改纵向叠放（加号在发送头上），消掉右列悬空空白。

import { useLayoutEffect, useRef, useState } from "react";
import { useI18n } from "@/lib/i18n";
import type { Attachment } from "@/lib/types";
import type { DocMode } from "@/features/chat/useImageAttachment";

const TA_MAX_PX = 128; // 多行长高上限（≈5 行，用户拍板「最多也不要太大」）

export default function Composer({
  disabled,
  webEnabled,
  webReady,
  thinkingEnabled,
  visionReady,
  visionOcrReady,
  attachment,
  preparing,
  docMode,
  onToggleWeb,
  onToggleThinking,
  onToggleDocMode,
  onPickFile,
  onRemoveAttachment,
  onSend,
  onStop,
  streaming,
}: {
  disabled?: boolean;
  webEnabled: boolean;
  webReady: boolean;
  thinkingEnabled: boolean;
  visionReady: boolean;
  visionOcrReady: boolean;
  attachment: Attachment | null;
  preparing: boolean;
  docMode: DocMode;
  onToggleWeb: () => void;
  onToggleThinking: () => void;
  onToggleDocMode: () => void;
  onPickFile: (file: File) => void;
  onRemoveAttachment: () => void;
  onSend: (text: string) => void;
  onStop: () => void;
  streaming: boolean;
}) {
  const { t } = useI18n();
  const [text, setText] = useState("");
  const [grown, setGrown] = useState(false); // 多行长高态：右侧按钮横排→纵向叠放
  const composingRef = useRef(false); // IME 组词中不触发发送
  const fileRef = useRef<HTMLInputElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  // 自动长高：内容变化时按 scrollHeight 伸展，封顶 TA_MAX_PX（超出内部滚动）；
  // 超过一行高（按钮 36px）即视为多行，切换右侧按钮布局
  useLayoutEffect(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = "auto";
    const h = Math.min(el.scrollHeight, TA_MAX_PX);
    el.style.height = `${h}px`;
    setGrown(h > 44);
  }, [text]);

  const submit = () => {
    const value = text.trim();
    if (!value || disabled || streaming || preparing) return;
    onSend(value);
    setText("");
  };

  return (
    <div className="border-t border-line bg-base/80 p-3 backdrop-blur">
      <div className="mx-auto max-w-3xl">
        <div className="mb-2 flex flex-wrap gap-1.5">
          {t.examples.map((example) => (
            <button
              key={example}
              disabled={disabled || streaming}
              title={example} // 截断时悬浮看全文；点击仍发完整问题
              onClick={() => onSend(example)}
              className="max-w-full truncate rounded-full border border-line px-2.5 py-1 text-[11px] text-dim transition hover:border-sky-800 hover:text-sky-300 disabled:opacity-40"
            >
              {example}
            </button>
          ))}
        </div>
        {(preparing || attachment) && (
          <div className="mb-2 flex items-center px-1 text-[11px]">
            {preparing ? (
              <span className="text-faint">{t.analyzing}</span>
            ) : attachment ? (
              <span
                title={attachment.error ? t.visionError : attachment.name}
                className={`inline-flex max-w-full items-center gap-1.5 rounded-full border px-2.5 py-1 ${
                  attachment.error ? "border-red-600 text-red-400" : "border-line text-dim"
                }`}
              >
                <span className="max-w-64 truncate">
                  {attachment.error ? t.visionError : attachment.name}
                </span>
                <button
                  onClick={onRemoveAttachment}
                  aria-label={t.remove}
                  className="text-faint hover:text-fg"
                >
                  ✕
                </button>
              </span>
            ) : null}
          </div>
        )}
        <div className="flex items-end gap-2 rounded-xl border border-line bg-panel p-1.5 focus-within:border-sky-800 focus-within:ring-1 focus-within:ring-sky-800/30">
          <textarea
            ref={taRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onCompositionStart={() => (composingRef.current = true)}
            onCompositionEnd={() => (composingRef.current = false)}
            onKeyDown={(e) => {
              // IME 防误发：组词中的 Enter 不发送
              if (e.key === "Enter" && !e.shiftKey && !composingRef.current) {
                e.preventDefault();
                submit();
              }
            }}
            rows={1}
            maxLength={4000}
            placeholder={t.placeholder}
            className="flex-1 resize-none bg-transparent px-2 py-1.5 text-sm text-fg outline-none placeholder:text-faint [&::-webkit-scrollbar]:w-1.5 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-line [&::-webkit-scrollbar-track]:bg-transparent"
          />
          {/* 多行时纵排（加号在发送头上），单行时横排；items-stretch 让加号与发送同宽 */}
          <div
            className={
              grown
                ? "flex shrink-0 flex-col items-stretch gap-1.5"
                : "flex shrink-0 items-center gap-2"
            }
          >
            {visionReady && (
              <>
                <button
                  onClick={() => fileRef.current?.click()}
                  disabled={disabled || streaming || preparing}
                  title={t.attach}
                  aria-label={t.attach}
                  className={`shrink-0 rounded-lg border border-line px-3 py-2 text-base leading-none text-dim transition hover:border-sky-800 hover:text-sky-300 disabled:cursor-not-allowed disabled:opacity-40 ${
                    grown ? "w-full" : ""
                  }`}
                >
                  +
                </button>
                <input
                  ref={fileRef}
                  type="file"
                  accept="image/*"
                  className="hidden"
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (file) onPickFile(file);
                    e.target.value = ""; // 同名文件可重复选择
                  }}
                />
              </>
            )}
            {streaming ? (
              <button
                onClick={onStop}
                className="shrink-0 rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-500 dark:bg-red-900/70 dark:text-red-200 dark:hover:bg-red-800/70"
              >
                {t.stop}
              </button>
            ) : (
              <button
                onClick={submit}
                disabled={disabled || !text.trim()}
                className="shrink-0 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {t.send}
              </button>
            )}
          </div>
        </div>
        <div className="mt-1.5 flex items-center gap-3 px-1 text-[11px]">
          {webReady && <Toggle label={t.webSearch} on={webEnabled} onChange={onToggleWeb} />}
          <Toggle label={t.deepThink} on={thinkingEnabled} onChange={onToggleThinking} />
          {attachment && visionOcrReady && (
            <Toggle
              label={t.parseAsDoc}
              hint={t.parseAsDocHint}
              on={docMode === "ocr"}
              onChange={onToggleDocMode}
            />
          )}
          <span className="ml-auto text-faint">{text.length}/4000</span>
        </div>
      </div>
    </div>
  );
}

function Toggle({ label, hint, on, onChange }: {
  label: string;
  hint?: string;
  on: boolean;
  onChange: () => void;
}) {
  return (
    <button onClick={onChange} title={hint} className="flex items-center gap-1.5 text-dim hover:text-fg">
      <span
        className={`relative inline-flex h-4 w-7 items-center rounded-full transition ${on ? "bg-accent" : "bg-line"}`}
      >
        <span
          className={`absolute h-3 w-3 rounded-full bg-white transition-all ${on ? "left-3.5" : "left-0.5"}`}
        />
      </span>
      {label}
    </button>
  );
}
