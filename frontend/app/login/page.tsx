"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { BookOpen, Loader2, Send } from "lucide-react";
import { apiLogin, apiRegister, apiSendEmailCode } from "@/lib/auth";
import { useAuthStore } from "@/stores/auth";

// Browser login/register entry for account-based deployments. The backend
// decides independently whether registration and guest access are enabled.
export default function LoginPage() {
  const router = useRouter();
  const checked = useAuthStore((s) => s.checked);
  const authRequired = useAuthStore((s) => s.authRequired);
  const registrationOpen = useAuthStore((s) => s.registrationOpen);
  const guestAccess = useAuthStore((s) => s.guestAccess);
  const emailRequirement = useAuthStore((s) => s.emailRequirement);
  const token = useAuthStore((s) => s.token);
  const setAuth = useAuthStore((s) => s.setAuth);
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [codeCooldown, setCodeCooldown] = useState(0);
  const [sendingCode, setSendingCode] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [passwordChanged, setPasswordChanged] = useState(false);

  const registerMode = mode === "register";
  const needsEmail = registerMode && emailRequirement !== "none";
  const needsCode = registerMode && emailRequirement === "verify";

  useEffect(() => {
    void useAuthStore.getState().hydrate();
    setPasswordChanged(new URLSearchParams(window.location.search).get("password_changed") === "1");
  }, []);

  useEffect(() => {
    if (codeCooldown <= 0) return;
    const timer = setTimeout(() => setCodeCooldown((value) => value - 1), 1_000);
    return () => clearTimeout(timer);
  }, [codeCooldown]);

  useEffect(() => {
    if (!checked) return;
    if (!authRequired || token) router.replace("/chat");
  }, [authRequired, checked, router, token]);

  const sendCode = async () => {
    if (!email.trim() || sendingCode || codeCooldown > 0) return;
    setSendingCode(true);
    setError("");
    try {
      await apiSendEmailCode(email.trim());
      setCodeCooldown(60);
    } catch (err) {
      setError(err instanceof Error ? err.message : "验证码发送失败，请重试");
    } finally {
      setSendingCode(false);
    }
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setError("");
    setBusy(true);
    try {
      const result = mode === "login"
        ? await apiLogin(username, password)
        : await apiRegister(username, password, displayName,
            needsEmail ? email.trim() : "", needsCode ? code.trim() : "");
      setAuth(result.token, result.user);
      router.replace(result.user?.role === "administrator" ? "/admin/agent-keys" : "/chat");
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败，请重试");
    } finally {
      setBusy(false);
    }
  };

  if (!checked) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-bg">
        <Loader2 className="h-8 w-8 animate-spin text-accent" />
      </div>
    );
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-bg px-4">
      {/* 顶部主色光晕，给纯功能页一点品牌氛围 */}
      <div aria-hidden className="pointer-events-none absolute -top-40 left-1/2 h-96 w-[42rem] max-w-[100vw] -translate-x-1/2 rounded-full bg-accent/10 blur-[120px]" />
      <div className="relative w-full max-w-sm rounded-2xl border border-border-light bg-surface p-8 shadow-lg">
        <div className="mb-6 flex flex-col items-center">
          <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-gradient-to-br from-accent to-accent-hover shadow-md shadow-accent/25">
            <BookOpen className="h-6 w-6 text-white" />
          </div>
          <h1 className="text-xl font-bold tracking-tight text-fg">Paper Agent</h1>
          <p className="mt-1 text-xs text-muted">{mode === "login" ? "登录以继续使用" : "注册新账号"}</p>
        </div>

        {passwordChanged && (
          <p className="mb-3 rounded-lg border border-success/30 bg-success/10 px-3 py-2 text-xs text-fg-secondary">
            密码已修改，所有旧会话均已退出，请使用新密码登录。
          </p>
        )}

        <form onSubmit={submit} className="space-y-3">
          <div>
            <label className="mb-1 block text-xs font-medium text-fg-secondary">用户名</label>
            <input value={username} onChange={(event) => setUsername(event.target.value)} required autoFocus autoComplete="username" className="w-full rounded-lg border border-border-light bg-bg px-3 py-2 text-sm text-fg outline-none transition-colors focus:border-accent/50" placeholder="2-32 个字符" />
          </div>
          {registerMode && (
            <div>
              <label className="mb-1 block text-xs font-medium text-fg-secondary">昵称（可选）</label>
              <input value={displayName} onChange={(event) => setDisplayName(event.target.value)} className="w-full rounded-lg border border-border-light bg-bg px-3 py-2 text-sm text-fg outline-none transition-colors focus:border-accent/50" placeholder="默认与用户名相同" />
            </div>
          )}
          {needsEmail && (
            <div>
              <label className="mb-1 block text-xs font-medium text-fg-secondary">
                邮箱{emailRequirement === "collect" ? "" : "（用于接收验证码）"}
              </label>
              <input type="email" value={email} onChange={(event) => setEmail(event.target.value)} required autoComplete="email" className="w-full rounded-lg border border-border-light bg-bg px-3 py-2 text-sm text-fg outline-none transition-colors focus:border-accent/50" placeholder="you@example.com" />
            </div>
          )}
          {needsCode && (
            <div>
              <label className="mb-1 block text-xs font-medium text-fg-secondary">验证码</label>
              <div className="flex gap-2">
                <input value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, "").slice(0, 6))} required inputMode="numeric" maxLength={6} className="w-full rounded-lg border border-border-light bg-bg px-3 py-2 text-sm tracking-widest text-fg outline-none transition-colors focus:border-accent/50" placeholder="6 位数字" />
                <button type="button" onClick={() => void sendCode()} disabled={!email.trim() || sendingCode || codeCooldown > 0}
                  className="flex h-[38px] shrink-0 items-center gap-1.5 rounded-lg border border-border-light px-3 text-xs text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg disabled:opacity-50">
                  {sendingCode ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
                  {codeCooldown > 0 ? `${codeCooldown}s 后重发` : "发送验证码"}
                </button>
              </div>
            </div>
          )}
          <div>
            <label className="mb-1 block text-xs font-medium text-fg-secondary">密码</label>
            <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} required autoComplete={mode === "login" ? "current-password" : "new-password"} className="w-full rounded-lg border border-border-light bg-bg px-3 py-2 text-sm text-fg outline-none transition-colors focus:border-accent/50" placeholder={registerMode ? "至少 8 位" : ""} />
          </div>

          {error && <p className="rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-fg-secondary">{error}</p>}

          <button type="submit" disabled={busy} className="flex w-full items-center justify-center gap-2 rounded-lg bg-accent py-2.5 text-sm font-medium text-white transition-colors hover:bg-accent-hover disabled:opacity-60">
            {busy && <Loader2 className="h-4 w-4 animate-spin" />}
            {mode === "login" ? "登录" : "注册并登录"}
          </button>
        </form>

        {registrationOpen && (
          <p className="mt-4 text-center text-xs text-muted">
            {mode === "login" ? "还没有账号？" : "已有账号？"}
            <button onClick={() => { setMode(mode === "login" ? "register" : "login"); setError(""); }} className="ml-1 text-accent hover:underline">
              {mode === "login" ? "注册" : "登录"}
            </button>
          </p>
        )}
        {guestAccess && (
          <p className="mt-2 text-center text-xs text-muted/70">
            <a href="/chat" className="hover:text-accent">先不登录，以游客身份使用 →</a>
          </p>
        )}
      </div>
    </div>
  );
}
