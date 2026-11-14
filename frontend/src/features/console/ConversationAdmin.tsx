"use client";

// 控制台·全库会话管理（B12）：属主列 + 标题搜索 + 多选批量删（交互照抄聊天侧栏）
// + 清空空壳两步式（dry_run 预览计数与样例 → 确认执行）。
// 数据来自 /api/system/conversations（admin 旁路），与聊天页可见域三元组无关。

import { useCallback, useEffect, useRef, useState } from "react";
import {
  adminDeleteConversation,
  listAllConversations,
  purgeEmptyConversations,
  type AdminConvRow,
} from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Overlay } from "./UserAdmin";

export default function ConversationAdmin() {
  const { t } = useI18n();
  const [rows, setRows] = useState<AdminConvRow[]>([]);
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(false);
  const [selecting, setSelecting] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [confirmBatch, setConfirmBatch] = useState(false);
  const [purgePreview, setPurgePreview] = useState<{ count: number; samples: string[] } | null>(
    null,
  );
  const [purgeResult, setPurgeResult] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async (query: string) => {
    setLoading(true);
    try {
      setRows((await listAllConversations(200, query)).conversations);
    } catch (err) {
      setNotice(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load("");
  }, [load]);

  const onSearch = (value: string) => {
    setQ(value);
    if (debounce.current) clearTimeout(debounce.current);
    debounce.current = setTimeout(() => load(value.trim()), 300);
  };

  const toggle = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const doBatchRemove = async () => {
    setBusy(true);
    try {
      for (const id of selectedIds) {
        try {
          await adminDeleteConversation(id);
        } catch {
          /* 单条失败不中断批删（与侧栏一致） */
        }
      }
      setSelectedIds(new Set());
      setSelecting(false);
      await load(q.trim());
    } finally {
      setBusy(false);
      setConfirmBatch(false);
    }
  };

  const previewPurge = async () => {
    setBusy(true);
    setNotice("");
    try {
      const r = await purgeEmptyConversations(true);
      setPurgePreview({ count: r.count ?? 0, samples: r.samples ?? [] });
    } catch (err) {
      setNotice(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const executePurge = async () => {
    setBusy(true);
    try {
      const r = await purgeEmptyConversations(false);
      setPurgeResult(t.purgeDone(r.deleted ?? 0));
      await load(q.trim());
    } catch (err) {
      setNotice(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
      setPurgePreview(null);
    }
  };

  const ownerLabel = (row: AdminConvRow) =>
    row.owner ?? t.machineDomain;

  const btnGhost =
    "rounded-lg border border-line px-2 py-1 text-xs text-dim hover:text-fg disabled:cursor-not-allowed disabled:opacity-40";

  return (
    <section className="rounded-xl border border-line bg-panel p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-fg">{t.convAdminTitle}</h3>
        <input
          value={q}
          onChange={(e) => onSearch(e.target.value)}
          placeholder={t.convSearchPlaceholder}
          className="min-w-40 flex-1 rounded-lg border border-line bg-base px-3 py-1.5 text-xs text-fg placeholder:text-faint focus:outline-none"
        />
        <button
          className={`rounded-lg px-2 py-1 text-xs transition ${
            selecting
              ? "bg-accent text-white"
              : "border border-line text-dim hover:text-fg"
          }`}
          onClick={() => {
            setSelecting((v) => !v);
            setSelectedIds(new Set());
          }}
        >
          ☑ {t.convSelect}
        </button>
        <button className={btnGhost} disabled={busy} onClick={previewPurge}>
          {t.purgeEmptyBtn}
        </button>
      </div>

      {selecting && (
        <div className="mt-2 flex items-center gap-2 rounded-lg border border-line bg-base px-3 py-1.5 text-xs">
          <span className="text-dim">{t.convSelectedCount(selectedIds.size)}</span>
          <button
            className="text-dim hover:text-fg"
            onClick={() => setSelectedIds(new Set(rows.map((r) => r.id)))}
          >
            {t.convSelectAll}
          </button>
          <button
            className="text-red-600 hover:opacity-80 dark:text-red-300 disabled:opacity-40"
            disabled={selectedIds.size === 0 || busy}
            onClick={() => setConfirmBatch(true)}
          >
            {t.deleteSelected}
          </button>
          <button
            className="ml-auto text-faint hover:text-fg"
            onClick={() => {
              setSelecting(false);
              setSelectedIds(new Set());
            }}
          >
            {t.cancel}
          </button>
        </div>
      )}

      <div className="mt-3 max-h-80 overflow-y-auto">
        {rows.length === 0 && !loading && (
          <p className="py-3 text-center text-xs text-faint">{t.noConvMatch}</p>
        )}
        <ul className="divide-y divide-line/60 text-xs">
          {rows.map((row) => (
            <li
              key={row.id}
              className="flex items-center gap-2 py-1.5"
              onClick={() => selecting && toggle(row.id)}
              role={selecting ? "button" : undefined}
            >
              {selecting && (
                <span className="w-4 text-center text-accent">
                  {selectedIds.has(row.id) ? "☑" : "☐"}
                </span>
              )}
              <span className="min-w-0 flex-1 truncate text-fg" title={row.title}>
                {row.title}
              </span>
              {!row.has_reply && (
                <span className="rounded bg-amber-500/10 px-1.5 py-0.5 text-amber-600 dark:text-amber-300">
                  {t.noReplyBadge}
                </span>
              )}
              <span className="shrink-0 rounded bg-base px-1.5 py-0.5 text-faint">
                {ownerLabel(row)}
              </span>
              <span className="w-32 shrink-0 text-right text-faint">
                {new Date(row.updated_at).toLocaleString()}
              </span>
            </li>
          ))}
        </ul>
      </div>
      {(notice || purgeResult) && (
        <p className="mt-2 text-xs text-dim">
          {purgeResult || notice}
        </p>
      )}

      {confirmBatch && (
        <Overlay onClose={() => setConfirmBatch(false)} title={t.batchDeleteConvTitle}>
          <p className="text-xs leading-5 text-dim">{t.batchDeleteConvBody(selectedIds.size)}</p>
          <div className="mt-4 flex justify-end gap-2">
            <button onClick={() => setConfirmBatch(false)} className={btnGhost}>
              {t.cancel}
            </button>
            <button
              disabled={busy}
              onClick={doBatchRemove}
              className="rounded-lg bg-red-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-red-500 dark:bg-red-800 dark:hover:bg-red-700"
            >
              {t.deleteSelected}
            </button>
          </div>
        </Overlay>
      )}

      {purgePreview && (
        <Overlay onClose={() => setPurgePreview(null)} title={t.purgePreviewTitle}>
          <p className="text-xs leading-5 text-dim">{t.purgePreviewCount(purgePreview.count)}</p>
          <ul className="mt-2 max-h-40 overflow-y-auto rounded-lg border border-line bg-panel p-2 text-xs text-dim">
            {purgePreview.samples.map((s, i) => (
              <li key={i} className="truncate py-0.5">
                · {s}
              </li>
            ))}
          </ul>
          <div className="mt-4 flex justify-end gap-2">
            <button onClick={() => setPurgePreview(null)} className={btnGhost}>
              {t.cancel}
            </button>
            <button
              disabled={busy || purgePreview.count === 0}
              onClick={executePurge}
              className="rounded-lg bg-red-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-red-500 dark:bg-red-800 dark:hover:bg-red-700 disabled:opacity-40"
            >
              {t.purgeExecuteBtn}
            </button>
          </div>
        </Overlay>
      )}
    </section>
  );
}
