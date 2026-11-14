"use client";

// 控制台·用户管理（B12）：用户表 + 建号 / 重置密码（吊销会话）/ 停用·启用。
// 对话框仿 SettingsDialog overlay 模式；自己那行的停用按钮禁用态（后端亦有 4001 防呆）。

import { useCallback, useEffect, useState } from "react";
import {
  createUser,
  getUsers,
  resetPassword,
  setUserDisabled,
  type UserRow,
} from "@/lib/api";
import { getUser } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";

type Dialog =
  | { mode: "create" }
  | { mode: "reset"; username: string }
  | { mode: "disable"; username: string }
  | null;

export default function UserAdmin() {
  const { t } = useI18n();
  const [users, setUsers] = useState<UserRow[]>([]);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<"admin" | "user">("user");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const me = getUser();
  const myName = me?.username ?? "";

  const load = useCallback(async () => {
    try {
      setUsers((await getUsers()).users);
    } catch {
      /* 401/403 由 jfetch 统一处理 */
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const run = async (fn: () => Promise<void>, close: boolean = true) => {
    setBusy(true);
    try {
      await fn();
      if (close) setDialog(null);
    } catch (err) {
      setNotice(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const submitCreate = () =>
    run(async () => {
      await createUser(username.trim(), password, role);
      setNotice(`${t.userCreated}: ${username.trim()}`);
      setUsername("");
      setPassword("");
      setRole("user");
      await load();
    });

  const submitReset = (name: string) =>
    run(async () => {
      const r = await resetPassword(name, password);
      setNotice(t.resetDone(r.sessions_revoked));
      setPassword("");
      await load();
    });

  const submitDisable = async (name: string, disabled: boolean) => {
    setBusy(true);
    try {
      await setUserDisabled(name, disabled);
      await load();
    } catch (err) {
      setNotice(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
    setDialog(null);
  };

  const inputCls =
    "w-full rounded-lg border border-line bg-panel px-3 py-2 text-sm text-fg placeholder:text-faint focus:outline-none";
  const btnGhost =
    "rounded-lg border border-line px-2 py-1 text-xs text-dim hover:text-fg disabled:cursor-not-allowed disabled:opacity-40";

  return (
    <section className="rounded-xl border border-line bg-panel p-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-fg">{t.usersTitle}</h3>
        <button
          onClick={() => {
            setNotice("");
            setDialog({ mode: "create" });
          }}
          className="rounded-lg bg-accent px-3 py-1 text-xs font-medium text-white hover:opacity-90"
        >
          {t.userCreateBtn}
        </button>
      </div>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead>
            <tr className="border-b border-line text-faint">
              <th className="py-1.5 pr-3 font-normal">{t.usernameLabel}</th>
              <th className="py-1.5 pr-3 font-normal">{t.userRoleCol}</th>
              <th className="py-1.5 pr-3 font-normal">{t.userSessionsCol}</th>
              <th className="py-1.5 pr-3 font-normal">{t.userStatusCol}</th>
              <th className="py-1.5 font-normal"></th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.username} className="border-b border-line/60 last:border-0">
                <td className="py-1.5 pr-3 text-fg">{u.username}</td>
                <td className="py-1.5 pr-3 text-dim">{u.role === "admin" ? t.roleAdmin : t.roleUser}</td>
                <td className="py-1.5 pr-3 text-dim">{u.sessions}</td>
                <td className="py-1.5 pr-3">
                  {u.disabled ? (
                    <span className="rounded bg-red-500/10 px-1.5 py-0.5 text-red-600 dark:text-red-300">
                      {t.userDisabledState}
                    </span>
                  ) : (
                    <span className="rounded bg-emerald-500/10 px-1.5 py-0.5 text-emerald-600 dark:text-emerald-300">
                      {t.userActiveState}
                    </span>
                  )}
                </td>
                <td className="py-1.5">
                  <div className="flex justify-end gap-1.5">
                    <button
                      className={btnGhost}
                      onClick={() => {
                        setNotice("");
                        setPassword("");
                        setDialog({ mode: "reset", username: u.username });
                      }}
                    >
                      {t.userResetBtn}
                    </button>
                    {u.disabled ? (
                      <button
                        className={btnGhost}
                        disabled={busy}
                        onClick={() => submitDisable(u.username, false)}
                      >
                        {t.userEnableBtn}
                      </button>
                    ) : (
                      <button
                        className={btnGhost}
                        disabled={busy || u.username === myName}
                        title={u.username === myName ? t.disableBody(u.username) : undefined}
                        onClick={() => setDialog({ mode: "disable", username: u.username })}
                      >
                        {t.userDisableBtn}
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {notice && <p className="mt-2 text-xs text-dim">{notice}</p>}

      {dialog?.mode === "create" && (
        <Overlay onClose={() => setDialog(null)} title={t.createUserTitle}>
          <label className="block text-xs text-faint">
            {t.usernameLabel}
            <input
              autoFocus
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className={`mt-1 ${inputCls}`}
            />
          </label>
          <label className="mt-3 block text-xs text-faint">
            {t.passwordLabel}
            <input
              type="password"
              value={password}
              placeholder={t.passwordPlaceholder}
              onChange={(e) => setPassword(e.target.value)}
              className={`mt-1 ${inputCls}`}
            />
          </label>
          <div className="mt-3">
            <p className="text-xs text-faint">{t.userRoleCol}</p>
            <div className="mt-1 flex gap-1 rounded-lg border border-line bg-base p-1">
              {(["user", "admin"] as const).map((r) => (
                <button
                  key={r}
                  onClick={() => setRole(r)}
                  className={`flex-1 rounded-md px-2 py-1.5 text-xs transition ${
                    role === r ? "bg-accent text-white" : "text-dim hover:text-fg"
                  }`}
                >
                  {r === "user" ? t.roleUser : t.roleAdmin}
                </button>
              ))}
            </div>
          </div>
          {notice && <p className="mt-2 text-xs text-red-600 dark:text-red-300">{notice}</p>}
          <div className="mt-4 flex justify-end gap-2">
            <button onClick={() => setDialog(null)} className={btnGhost}>
              {t.cancel}
            </button>
            <button
              disabled={busy || username.trim().length < 1 || password.length < 4}
              onClick={submitCreate}
              className="rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-40"
            >
              {t.userCreateBtn}
            </button>
          </div>
        </Overlay>
      )}

      {dialog?.mode === "reset" && (
        <Overlay onClose={() => setDialog(null)} title={t.resetTitle}>
          <p className="text-xs leading-5 text-dim">{t.resetBody(dialog.username)}</p>
          <input
            autoFocus
            type="password"
            value={password}
            placeholder={t.passwordPlaceholder}
            onChange={(e) => setPassword(e.target.value)}
            className={`mt-3 ${inputCls}`}
          />
          {notice && <p className="mt-2 text-xs text-dim">{notice}</p>}
          <div className="mt-4 flex justify-end gap-2">
            <button onClick={() => setDialog(null)} className={btnGhost}>
              {t.cancel}
            </button>
            <button
              disabled={busy || password.length < 4}
              onClick={() => submitReset(dialog.username)}
              className="rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-40"
            >
              {t.userResetBtn}
            </button>
          </div>
        </Overlay>
      )}

      {dialog?.mode === "disable" && (
        <Overlay onClose={() => setDialog(null)} title={t.disableTitle}>
          <p className="text-xs leading-5 text-dim">{t.disableBody(dialog.username)}</p>
          <div className="mt-4 flex justify-end gap-2">
            <button onClick={() => setDialog(null)} className={btnGhost}>
              {t.cancel}
            </button>
            <button
              disabled={busy}
              onClick={() => submitDisable(dialog.username, true)}
              className="rounded-lg bg-red-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-red-500 dark:bg-red-800 dark:hover:bg-red-700"
            >
              {t.userDisableBtn}
            </button>
          </div>
        </Overlay>
      )}
    </section>
  );
}

export function Overlay({
  title,
  children,
  onClose,
}: {
  title: string;
  children: React.ReactNode;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-sm rounded-2xl border border-line bg-base p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-fg">{title}</h3>
          <button onClick={onClose} className="text-faint hover:text-fg" title="✕">
            ✕
          </button>
        </div>
        <div className="mt-3">{children}</div>
      </div>
    </div>
  );
}
