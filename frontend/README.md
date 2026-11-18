# bidqa 前端

招投标智能问答机器人前端（Vite + React 19 单页应用）：问答页 + 评估页 + 系统监控抽屉 + 控制台。
页面结构与组件地图见 [`docs/04_项目结构流程图.md`](../docs/04_项目结构流程图.md) §8；SSE 事件契约见 [`docs/01_系统框架设计.md`](../docs/01_系统框架设计.md) §3.4。

## 技术栈

Vite + React 19（react-router-dom 五路由）+ TypeScript + Tailwind 4。
界面文案唯一来源是 `src/lib/i18n.tsx`（中英双语，组件内禁止写死文案）。

## 常用命令

```bash
npm install          # 安装依赖
npm run dev          # 开发服务 http://127.0.0.1:3000（需后端已在 8000 运行）
npm run typecheck    # 类型检查（CI 同款，用 node_modules/.bin/tsc 防同名恶搞包）
npm run build        # 生产构建（tsc -b && vite build，产物在 dist/）
npm run preview      # 本地预览构建产物
```

## 注意事项

- SSE 流式请求**直连后端 127.0.0.1:8000**（`src/lib/sse.ts`），不走 dev 代理——代理会把整个 SSE 缓冲到最后一次性吐出
- 后端基址可用构建期变量 `VITE_API_BASE` 覆盖（`src/vite-env.d.ts`）；`/api/*` 的 dev 代理在 `vite.config.ts` `server.proxy`
- 路由壳在 `src/App.tsx`（react-router-dom）；页面组件在 `src/app/*/page.tsx`；入口 `src/main.tsx` + `index.html`
- 旧流防护三件套在 `src/app/page.tsx`：AbortController + 请求序号 + 会话归属校验，改动收尾逻辑前先读懂
