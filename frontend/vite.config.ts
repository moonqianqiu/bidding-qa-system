import { fileURLToPath, URL } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 手动分包：按依赖生态拆分 vendor chunk，消除主 chunk 超 500 kB 警告
// - react-vendor: react / react-dom / scheduler（框架核心）
// - router: react-router / react-router-dom（注意须先于 react 判断，避免前缀误匹配）
// - markdown: react-markdown / remark-gfm 及 micromark、mdast、hast 等全部传递依赖
// 未匹配到的 node_modules 模块返回 undefined，走 Rollup 默认归属
function manualChunks(id: string): string | undefined {
  if (!id.includes("node_modules")) return undefined;
  if (id.includes("node_modules/react-router")) return "router";
  if (
    id.includes("node_modules/react/") ||
    id.includes("node_modules/react-dom/") ||
    id.includes("node_modules/scheduler/")
  ) {
    return "react-vendor";
  }
  return "markdown";
}

// Vite 开发代理：/api/* → FastAPI 后端（对应原 next.config.ts 的 rewrites，路径原样透传）
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks,
      },
    },
  },
  server: {
    // 显式绑 127.0.0.1：Vite 7 默认只听 ::1，而文档/脚本口径是 127.0.0.1:3000
    host: "127.0.0.1",
    port: 3000,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
