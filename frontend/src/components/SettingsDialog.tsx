"use client";

// A1 设置对话框：外观（深/浅/跟随系统）+ 语言（中/英）。
// 全部受控于页面状态：点击即改全局并持久化（绝不"只改设置框自己"）。

import { useEffect } from "react";
import { useI18n } from "@/lib/i18n";
import type { Providers } from "@/lib/api";

export type ThemeMode = "dark" | "light" | "system";

const THEMES: ThemeMode[] = ["dark", "light", "system"];

export default function SettingsDialog({
  themeMode,
  onTheme,
  providers,
  provider,
  onProvider,
  onClose,
}: {
  themeMode: ThemeMode;
  onTheme: (mode: ThemeMode) => void;
  providers: Providers | null;
  provider: string; // "" = 默认槽位
  onProvider: (name: string) => void;
  onClose: () => void;
}) {
  const { lang, t, setLang } = useI18n();

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
        className="w-full max-w-sm rounded-2xl border border-line bg-base p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-fg">{t.settingsTitle}</h3>
          <button onClick={onClose} className="text-faint hover:text-fg" title="✕">
            ✕
          </button>
        </div>
        <div className="mt-4 space-y-4">
          <section>
            <p className="text-xs text-faint">{t.appearance}</p>
            <div className="mt-2 flex gap-1 rounded-lg border border-line bg-panel p-1">
              {THEMES.map((mode) => (
                <button
                  key={mode}
                  onClick={() => onTheme(mode)}
                  className={`flex-1 rounded-md px-2 py-1.5 text-xs transition ${
                    themeMode === mode ? "bg-accent text-white" : "text-dim hover:text-fg"
                  }`}
                >
                  {mode === "dark" ? t.themeDark : mode === "light" ? t.themeLight : t.themeSystem}
                </button>
              ))}
            </div>
          </section>
          <section>
            <p className="text-xs text-faint">{t.language}</p>
            <div className="mt-2 flex gap-1 rounded-lg border border-line bg-panel p-1">
              {(["zh", "en"] as const).map((l) => (
                <button
                  key={l}
                  onClick={() => setLang(l)}
                  className={`flex-1 rounded-md px-2 py-1.5 text-xs transition ${
                    lang === l ? "bg-accent text-white" : "text-dim hover:text-fg"
                  }`}
                >
                  {l === "zh" ? t.langZh : t.langEn}
                </button>
              ))}
            </div>
          </section>
          {!!providers?.profiles.length && (
            <section>
              <p className="text-xs text-faint">{t.modelLabel}</p>
              <div className="mt-2 flex flex-col gap-1 rounded-lg border border-line bg-panel p-1">
                <button
                  onClick={() => onProvider("")}
                  className={`rounded-md px-2 py-1.5 text-left text-xs transition ${
                    provider === "" ? "bg-accent text-white" : "text-dim hover:text-fg"
                  }`}
                >
                  {t.defaultModel(providers.default_model)}
                </button>
                {providers.profiles.map((p) => (
                  <button
                    key={p.name}
                    onClick={() => onProvider(p.name)}
                    className={`rounded-md px-2 py-1.5 text-left text-xs transition ${
                      provider === p.name ? "bg-accent text-white" : "text-dim hover:text-fg"
                    }`}
                  >
                    {p.name}
                    <span className={`ml-2 text-[10px] ${provider === p.name ? "text-white/70" : "text-faint"}`}>
                      {p.model}
                    </span>
                  </button>
                ))}
              </div>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
