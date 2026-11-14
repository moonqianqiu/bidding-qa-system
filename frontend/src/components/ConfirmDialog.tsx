"use client";

// 通用确认弹窗（防手滑）：删除会话等不可逆操作先过这里；Esc 关闭，取消键默认聚焦。

import { useEffect } from "react";

export default function ConfirmDialog({
  title,
  body,
  confirmLabel,
  cancelLabel,
  onConfirm,
  onClose,
}: {
  title: string;
  body: string;
  confirmLabel: string;
  cancelLabel: string;
  onConfirm: () => void;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-xs rounded-2xl border border-line bg-base p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-sm font-semibold text-fg">{title}</h3>
        <p className="mt-2 text-xs leading-5 text-dim">{body}</p>
        <div className="mt-4 flex justify-end gap-2">
          <button
            autoFocus
            onClick={onClose}
            className="rounded-lg border border-line px-3 py-1.5 text-xs text-dim hover:text-fg"
          >
            {cancelLabel}
          </button>
          <button
            onClick={() => {
              onConfirm();
              onClose();
            }}
            className="rounded-lg bg-red-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-red-500 dark:bg-red-800 dark:text-red-100 dark:hover:bg-red-700"
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
