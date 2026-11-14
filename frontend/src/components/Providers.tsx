"use client";

// 全局 i18n Provider：layout 是 server 组件，语言上下文在此包住全部路由
// （/、/eval、/login 都要用 useI18n；原先只在 page.tsx 内包，登录页拿不到）。
// SSR 首帧恒 zh，挂载后从 localStorage 同步（与 i18n.tsx 头注释同款约定）。

import { useCallback, useEffect, useState } from "react";
import { LangCtx, readLang, saveLang, tOf, type Dict, type Lang } from "@/lib/i18n";

export default function Providers({ children }: { children: React.ReactNode }) {
  const [lang, setLangState] = useState<Lang>("zh");
  useEffect(() => {
    setLangState(readLang());
    try {
      document.documentElement.lang = readLang() === "en" ? "en" : "zh-CN";
    } catch {
      /* 静默 */
    }
  }, []);
  const setLang = useCallback((l: Lang) => {
    setLangState(l);
    saveLang(l);
    try {
      document.documentElement.lang = l === "en" ? "en" : "zh-CN";
    } catch {
      /* 静默 */
    }
  }, []);
  const t: Dict = tOf(lang);
  return <LangCtx.Provider value={{ lang, t, setLang }}>{children}</LangCtx.Provider>;
}
