"use client";

// 登录页（docs/01 §3.1 用户体系）：账号密码登录 / 游客进入（运行时开关关闭时隐藏）。
// users_enabled=false（单机 token 模式）→ 直接进主页；已有会话 → 直接进主页。
// 凭据错误就地展示（jfetch 在 /login 不做 401 跳转，见 api.ts）。

import { useEffect, useState } from "react";
import { clearAuth, getToken, setAuth, type AuthUser } from "@/lib/auth";
import { getMe, jfetch, login, loginGuest, type SessionResponse } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

export default function LoginPage() {
  const { t } = useI18n();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [guestOn, setGuestOn] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (getToken()) {
      window.location.assign("/"); // 已登录（含游客）直接进
      return;
    }
    getMe()
      .then((m) => {
        if (!m.users_enabled) window.location.assign("/"); // 单机 token 模式无登录概念
        else setGuestOn(m.guest_enabled);
      })
      .catch(() => setError(t.loginFailed));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const go = (session: SessionResponse) => {
    setAuth(session.token, { username: session.username, role: session.role });
    window.location.assign("/");
  };

  const submit = async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (busy || !username.trim() || !password) return;
    setBusy(true);
    setError("");
    try {
      go(await login(username.trim(), password));
    } catch (err) {
      setError((err as Error).message || t.loginFailed);
      setBusy(false);
    }
  };

  const guest = async () => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      go(await loginGuest());
    } catch (err) {
      setError((err as Error).message || t.loginFailed);
      setBusy(false);
    }
  };

  return (
    <main className="flex min-h-dvh items-center justify-center bg-base px-4 text-fg">
      <div className="w-full max-w-sm rounded-2xl border border-line bg-panel p-6 shadow-lg">
        <div className="flex items-baseline justify-between">
          <h1 className="text-lg font-semibold">{t.loginTitle}</h1>
          <span className="text-xs text-faint">{t.loginSub}</span>
        </div>
        <form className="mt-5 space-y-3" onSubmit={submit}>
          <label className="block text-xs text-dim">
            {t.loginUsername}
            <input
              autoFocus
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="mt-1 w-full rounded-lg border border-line bg-base px-3 py-2 text-sm text-fg outline-none focus:border-sky-800"
            />
          </label>
          <label className="block text-xs text-dim">
            {t.loginPassword}
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1 w-full rounded-lg border border-line bg-base px-3 py-2 text-sm text-fg outline-none focus:border-sky-800"
            />
          </label>
          {error && (
            <p className="rounded-lg border border-red-900/60 bg-red-950/40 px-3 py-2 text-xs text-red-300">
              {t.loginFailed}：{error}
            </p>
          )}
          <button
            type="submit"
            disabled={busy || !username.trim() || !password}
            className="w-full rounded-lg bg-accent px-3 py-2 text-sm font-medium text-white hover:opacity-90 disabled:opacity-40"
          >
            {t.loginSubmit}
          </button>
        </form>
        {guestOn && (
          <>
            <div className="my-4 flex items-center gap-2 text-[10px] text-faint">
              <span className="h-px flex-1 bg-line" />or<span className="h-px flex-1 bg-line" />
            </div>
            <button
              onClick={guest}
              disabled={busy}
              className="w-full rounded-lg border border-line px-3 py-2 text-sm text-dim hover:border-sky-800 hover:text-sky-300 disabled:opacity-40"
            >
              {t.guestEnter}
            </button>
          </>
        )}
      </div>
    </main>
  );
}
