/// <reference types="vite/client" />

// Vite 环境变量类型（docs/01 §5：前端构建时覆盖项）
interface ImportMetaEnv {
  /** 后端 API 基址；缺省 http://127.0.0.1:8000（SSE 直连），设 "" 走同源代理 */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
