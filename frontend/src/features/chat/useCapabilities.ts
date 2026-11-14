"use client";

// 能力探测 hook（A3/D4 门控）：health 能力位 + providers 档位列表。
// vision/web 未就绪 = 按钮不出现（门控模式复用）；游客锁默认档（不拉 providers）。

import { useCallback, useEffect, useState } from "react";
import { getHealth, getProviders, type Providers } from "@/lib/api";

export function useCapabilities(isGuest: boolean) {
  const [healthOk, setHealthOk] = useState<boolean | null>(null);
  const [visionReady, setVisionReady] = useState(false); // A3：vision 就绪才显示上传按钮
  const [webReady, setWebReady] = useState(false); // D4/D5：联网供应商已配置才显示联网开关
  const [providers, setProviders] = useState<Providers | null>(null);
  const [provider, setProviderState] = useState(""); // "" = 默认槽位（A12）

  useEffect(() => {
    getHealth()
      .then((h) => {
        setHealthOk(h.status === "ok");
        setVisionReady(h.capabilities?.vision === true);
        setWebReady(h.capabilities?.web_search === true); // 留空不开启=按钮不出现
      })
      .catch(() => setHealthOk(false));
    try {
      setProviderState(localStorage.getItem("bidqa.provider") ?? "");
    } catch {
      /* 静默 */
    }
    if (!isGuest) getProviders().then(setProviders).catch(() => setProviders(null)); // 游客锁默认档=不拉取
  }, [isGuest]);

  const setProvider = useCallback((name: string) => {
    setProviderState(name);
    try {
      localStorage.setItem("bidqa.provider", name); // "" 表示默认槽位
    } catch {
      /* 静默 */
    }
  }, []);

  return { healthOk, visionReady, webReady, providers, provider, setProvider };
}
