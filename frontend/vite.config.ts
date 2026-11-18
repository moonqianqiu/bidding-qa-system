import { fileURLToPath, URL } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Vite 开发代理：/api/* → FastAPI 后端（对应原 next.config.ts 的 rewrites，路径原样透传）
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
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
