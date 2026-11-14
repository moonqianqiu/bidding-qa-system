import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  cacheComponents: true,
  partialPrefetching: true,
  // 开发模式放行 127.0.0.1（Next 16 默认视为跨域，会拦 /_next/hmr 导致 hydration 卡死）
  allowedDevOrigins: ["127.0.0.1", "localhost"],
  // 关掉左下角 N 调试按钮（Next 开发工具，非应用设置入口，防误认）
  devIndicators: false,
  async rewrites() {
    // 开发代理：/api/* → FastAPI 后端（docs/03 M0-3）
    return [
      {
        source: "/api/:path*",
        destination: "http://127.0.0.1:8000/api/:path*",
      },
    ];
  },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
