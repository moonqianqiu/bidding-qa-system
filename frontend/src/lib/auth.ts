"use client";

// 用户体系前端态（docs/01 §3.1）：token + 身份存 localStorage，请求头贯通由
// api.ts（jfetch）/ sse.ts（streamChat）消费；角色门控（徽标/导出/传图/档位）
// 由页面经 getUser() 判断。清态统一走 clearAuth（401/4031 由 api.ts/sse.ts 触发跳登录）。

export type Role = "admin" | "user" | "guest";
export type AuthUser = { username: string; role: Role };

const LS_TOKEN = "bidqa.token";
const LS_USER = "bidqa.user";

export function getToken(): string {
  try {
    return localStorage.getItem(LS_TOKEN) ?? "";
  } catch {
    return "";
  }
}

export function getUser(): AuthUser | null {
  try {
    const raw = localStorage.getItem(LS_USER);
    if (!raw) return null;
    const u = JSON.parse(raw) as AuthUser;
    return u.role === "admin" || u.role === "user" || u.role === "guest" ? u : null;
  } catch {
    return null;
  }
}

export function setAuth(token: string, user: AuthUser) {
  try {
    localStorage.setItem(LS_TOKEN, token);
    localStorage.setItem(LS_USER, JSON.stringify(user));
  } catch {
    /* 隐私模式等场景静默 */
  }
}

export function clearAuth() {
  try {
    localStorage.removeItem(LS_TOKEN);
    localStorage.removeItem(LS_USER);
  } catch {
    /* 静默 */
  }
}

export function isGuest(): boolean {
  return getUser()?.role === "guest";
}
