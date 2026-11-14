"use client";

// 主题 hook（A1）：三档状态 + 系统档跟随 OS 深浅切换。

import { useCallback, useEffect, useState } from "react";
import { applyTheme, readThemeMode, type ThemeMode } from "@/features/chat/theme";

export function useTheme() {
  const [themeMode, setThemeModeState] = useState<ThemeMode>("system");

  // 初始化：恢复持久化档位
  useEffect(() => {
    const mode = readThemeMode();
    setThemeModeState(mode);
    applyTheme(mode);
  }, []);

  // 跟随系统档：OS 深浅切换时即时跟随
  useEffect(() => {
    if (themeMode !== "system") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => applyTheme("system");
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [themeMode]);

  const setThemeMode = useCallback((mode: ThemeMode) => {
    setThemeModeState(mode);
    applyTheme(mode); // 即时生效 + 持久化（不"只改设置框"）
  }, []);

  return { themeMode, setThemeMode };
}
