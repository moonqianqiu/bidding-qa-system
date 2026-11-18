// SPA 入口（Vite + React 19）：替代原 Next App Router 的 layout.tsx ——
// Providers（全局 i18n 上下文）包住全部路由，路由壳见 App.tsx。
// 不用 StrictMode：问答流/轮询依赖 effect 恰好跑一次（与原 Next 行为一致）。

import { BrowserRouter } from "react-router-dom";
import Providers from "@/components/Providers";
import App from "@/App";
import { createRoot } from "react-dom/client";
import "./app/globals.css";

createRoot(document.getElementById("root")!).render(
  <Providers>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </Providers>,
);
