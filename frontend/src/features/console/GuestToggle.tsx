"use client";

// 控制台·游客开关（B12）：app_settings.guest_enabled 运行时切换。
// 即时生效语义在后端中间件（逐请求复核）——这里只负责拨开关并回显当前态。

import { useState } from "react";
import { setGuestEnabled } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

export default function GuestToggle({
  enabled,
  onChange,
}: {
  enabled: boolean;
  onChange: (v: boolean) => void;
}) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const flip = async () => {
    setBusy(true);
    setError("");
    try {
      const r = await setGuestEnabled(!enabled);
      onChange(r.guest_enabled);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="rounded-xl border border-line bg-panel p-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-fg">{t.guestToggleTitle}</h3>
        <button
          onClick={flip}
          disabled={busy}
          aria-pressed={enabled}
          className={`relative h-6 w-11 rounded-full transition disabled:opacity-50 ${
            enabled ? "bg-emerald-600" : "bg-zinc-400 dark:bg-zinc-600"
          }`}
          title={enabled ? t.guestOn : t.guestOff}
        >
          <span
            className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all ${
              enabled ? "left-[22px]" : "left-0.5"
            }`}
          />
        </button>
      </div>
      <p className="mt-2 text-xs text-dim">
        {enabled ? t.guestOn : t.guestOff}
        {error && <span className="ml-2 text-red-600 dark:text-red-300">{error}</span>}
      </p>
    </section>
  );
}
