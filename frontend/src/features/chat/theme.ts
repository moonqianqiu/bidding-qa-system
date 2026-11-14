"use client";

// 主题三档纯函数（A1 双主题：dark/light/system，system 跟随 OS 即时切换）。
// 从 page.tsx 抽出（C7）：落 <html> class + localStorage 持久化，隐私模式静默。

export const LS_THEME = "bidqa.theme";

export type ThemeMode = "system" | "dark" | "light";

export function readThemeMode(): ThemeMode {
  try {
    const t = localStorage.getItem(LS_THEME);
    return t === "dark" || t === "light" ? t : "system";
  } catch {
    return "system";
  }
}

/** 把主题档位落到 <html> class 并持久化；system 档按当前 OS 解析。 */
export function applyTheme(mode: ThemeMode) {
  let dark = mode === "dark";
  if (mode === "system") {
    try {
      dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    } catch {
      dark = true;
    }
  }
  document.documentElement.classList.toggle("dark", dark);
  try {
    localStorage.setItem(LS_THEME, mode);
  } catch {
    /* 隐私模式等场景静默 */
  }
}
