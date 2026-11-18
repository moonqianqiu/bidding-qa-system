// 路由壳（react-router-dom）：5 个路由与原 Next App Router 文件路由一一对应
// （/ 问答主页 · /login · /console · /eval · catch-all 404）。
// 页面组件原样保留在 src/app/*/page.tsx，仅外层从文件路由换成显式路由表。

import { Route, Routes } from "react-router-dom";
import Home from "@/app/page";
import LoginPage from "@/app/login/page";
import ConsolePage from "@/app/console/page";
import EvalPage from "@/app/eval/page";

// 404 兜底页：对齐 Next App Router 内置 404 的呈现（大号 404 + 提示语，居中）
function NotFound() {
  return (
    <main className="flex min-h-full flex-1 flex-col items-center justify-center gap-2 p-8 text-center">
      <p className="text-6xl font-semibold">404</p>
      <p className="text-dim">This page could not be found.</p>
    </main>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Home />} />
      <Route path="/login" element={<LoginPage />} />
      <Route path="/console" element={<ConsolePage />} />
      <Route path="/eval" element={<EvalPage />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
