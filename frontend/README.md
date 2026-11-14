# bidqa 前端

招投标智能问答机器人 Next.js 前端：问答页 + 评估页 + 系统监控抽屉。
页面结构与组件地图见 [`docs/04_项目结构流程图.md`](../docs/04_项目结构流程图.md) §8；SSE 事件契约见 [`docs/01_系统框架设计.md`](../docs/01_系统框架设计.md) §3.4。

## 技术栈

Next.js 16（App Router, Turbopack）+ React 19 + TypeScript + Tailwind 4。
界面文案唯一来源是 `src/lib/i18n.tsx`（中英双语，组件内禁止写死文案）。

## 常用命令

```bash
npm install          # 安装依赖
npm run dev          # 开发服务 http://127.0.0.1:3000（需后端已在 8000 运行）
npx tsc --noEmit     # 类型检查（CI 同款，用 node_modules/.bin/tsc 防同名恶搞包）
npm run build        # 生产构建
```

## 注意事项

- SSE 流式请求**直连后端 127.0.0.1:8000**（`src/lib/sse.ts`），不走 Next 代理——代理会把整个 SSE 缓冲到最后一次性吐出
- `next.config.ts` 的 `allowedDevOrigins: ["127.0.0.1"]` 防开发服务器 hydration 卡死，勿删
- 旧流防护三件套在 `src/app/page.tsx`：AbortController + 请求序号 + 会话归属校验，改动收尾逻辑前先读懂
