"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Check,
  Copy,
  KeyRound,
  Loader2,
  LockKeyhole,
  Plus,
  ShieldCheck,
  Trash2,
} from "lucide-react";

import {
  AdminHeader, AdminSection, HelpModal, InfoButton, type HelpEntry,
} from "@/components/admin/AdminUI";
import {
  AdminApiError,
  createAgentApiKey,
  listAgentApiKeys,
  revokeAgentApiKey,
  type AgentApiKeyItem,
} from "@/lib/admin-api";
import { apiChangePassword } from "@/lib/auth";
import { useAuthStore } from "@/stores/auth";

const HELP: Record<string, HelpEntry> = {
  create: {
    title: "创建 Agent API Key",
    entries: [
      ["这是什么密钥", "供清小搭广场以 Bearer Token 调用本服务的 /v1 OpenAI 兼容端点。它与 DeepSeek / VLM 服务商密钥完全不同，不要混用或粘贴到清小搭以外的地方。"],
      ["只显示一次", "完整密钥（pa_live_…）仅在创建成功时显示一次；关闭页面后无法再查看，丢了只能撤销重建。数据库只保存 SHA-256 哈希。"],
      ["长期有效", "密钥长期有效直到管理员主动撤销；撤销立即生效，清小搭下一次请求会立即收到 401。"],
      ["命名建议", "按用途命名（如「清小搭生产接入」「测试用」），方便在列表中区分与审计。"],
    ],
  },
  keys: {
    title: "已创建密钥列表",
    entries: [
      ["状态含义", "有效 = 清小搭可用它调用 /v1；已撤销 = 立即失效，历史记录保留供审计。"],
      ["撤销的后果", "不可恢复。使用该密钥的清小搭接入会立刻开始收到 401，需要创建新密钥并到清小搭后台更新。"],
      ["最近使用时间", "清小搭每次调用成功鉴权都会刷新；长期「从未」的密钥可以安全撤销。"],
    ],
  },
  password: {
    title: "修改管理员密码",
    entries: [
      ["生效方式", "修改成功后会撤销该账号的全部浏览器登录令牌，所有已登录的管理页会跳回登录页，需用新密码重新登录。"],
      ["密码要求", "至少 8 位；建议字母 + 数字 + 符号组合。密码经 PBKDF2 加盐哈希存储。"],
      ["忘记密码怎么办", "在服务器上重新运行 scripts/bootstrap_administrator.py 按提示重置（幂等，不会重复建号）。"],
    ],
  },
};

function formatTime(value: number | null): string {
  if (!value) return "从未";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value * 1000));
}

export default function AgentKeysAdminPage() {
  const router = useRouter();
  const checked = useAuthStore((s) => s.checked);
  const user = useAuthStore((s) => s.user);
  const signOut = useAuthStore((s) => s.signOut);
  const [items, setItems] = useState<AgentApiKeyItem[]>([]);
  const [name, setName] = useState("清小搭生产接入");
  const [newKey, setNewKey] = useState("");
  const [copied, setCopied] = useState(false);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [nextPassword, setNextPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [changingPassword, setChangingPassword] = useState(false);
  const [helpItem, setHelpItem] = useState<HelpEntry | null>(null);

  useEffect(() => {
    void useAuthStore.getState().hydrate();
  }, []);

  const handleApiError = useCallback((err: unknown) => {
    if (err instanceof AdminApiError && err.status === 401) {
      signOut();
      router.replace("/login");
      return;
    }
    setError(err instanceof Error ? err.message : "请求失败，请重试");
  }, [router, signOut]);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setItems(await listAgentApiKeys());
    } catch (err) {
      handleApiError(err);
    } finally {
      setLoading(false);
    }
  }, [handleApiError]);

  useEffect(() => {
    if (!checked) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (user.role !== "administrator") {
      setLoading(false);
      return;
    }
    void refresh();
  }, [checked, user, refresh, router]);

  const createKey = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!name.trim() || creating) return;
    setCreating(true);
    setError("");
    setNewKey("");
    try {
      const result = await createAgentApiKey(name.trim());
      setItems((current) => [result.item, ...current]);
      setNewKey(result.key);
      setCopied(false);
    } catch (err) {
      handleApiError(err);
    } finally {
      setCreating(false);
    }
  };

  const copyKey = async () => {
    await navigator.clipboard.writeText(newKey);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  };

  const revokeKey = async (item: AgentApiKeyItem) => {
    if (!window.confirm(`确认撤销“${item.name}”？撤销后清小搭将立即无法使用该密钥。`)) return;
    setError("");
    try {
      await revokeAgentApiKey(item.id);
      await refresh();
    } catch (err) {
      handleApiError(err);
    }
  };

  const changePassword = async (event: React.FormEvent) => {
    event.preventDefault();
    setError("");
    if (nextPassword !== confirmPassword) {
      setError("两次输入的新密码不一致");
      return;
    }
    setChangingPassword(true);
    try {
      await apiChangePassword(currentPassword, nextPassword);
      signOut();
      router.replace("/login?password_changed=1");
    } catch (err) {
      setError(err instanceof Error ? err.message : "密码修改失败");
    } finally {
      setChangingPassword(false);
    }
  };

  if (!checked || (user?.role === "administrator" && loading)) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-bg">
        <Loader2 className="h-8 w-8 animate-spin text-accent" />
      </div>
    );
  }

  if (!user || user.role !== "administrator") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-bg px-4">
        <div className="max-w-md rounded-2xl border border-border-light bg-surface p-8 text-center">
          <ShieldCheck className="mx-auto mb-3 h-10 w-10 text-muted" />
          <h1 className="text-lg font-semibold text-fg">无权访问管理页面</h1>
          <p className="mt-2 text-sm text-muted">只有 administrator 角色可以管理 Agent API Key。</p>
          <button onClick={() => router.replace("/chat")} className="mt-5 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white">
            返回对话
          </button>
        </div>
      </div>
    );
  }

  return (
    <main className="min-h-screen bg-bg px-4 py-8 text-fg sm:px-6">
      <div className="mx-auto max-w-5xl space-y-6">
        <AdminHeader title="Agent 接入管理" icon={<ShieldCheck className="h-5 w-5" />}
          subtitle={`管理员：${user.display_name || user.username}`}
          current="/admin/agent-keys" onRefresh={() => void refresh()} refreshing={loading} />

        {error && <p className="rounded-lg border border-warning/30 bg-warning/10 px-4 py-3 text-sm text-fg-secondary">{error}</p>}

        <AdminSection title="长期 Agent API Key" icon={<KeyRound className="h-5 w-5 text-accent" />}
          info={<InfoButton onClick={() => setHelpItem(HELP.create)} label="创建密钥说明" />}>
          <p className="mb-4 text-xs leading-relaxed text-muted">供清小搭以 Bearer Token 调用 <code>/v1</code>。密钥长期有效，直到管理员主动撤销；它与 DeepSeek/VLM 服务商密钥完全不同。</p>
          <form onSubmit={createKey} className="flex flex-col gap-2 sm:flex-row">
            <input value={name} onChange={(event) => setName(event.target.value)} maxLength={80} required className="h-10 min-w-0 flex-1 rounded-lg border border-border-light bg-bg px-3 text-sm outline-none focus:border-accent/50" placeholder="密钥用途名称" />
            <button disabled={creating} className="flex h-10 items-center justify-center gap-1.5 rounded-lg bg-accent px-4 text-sm font-medium text-white hover:bg-accent-hover disabled:opacity-60">
              {creating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />} 创建密钥
            </button>
          </form>

          {newKey && (
            <div className="mt-4 rounded-xl border border-warning/30 bg-warning/10 p-4">
              <p className="text-sm font-semibold">请立即复制：完整密钥只显示这一次</p>
              <p className="mt-1 text-xs text-muted">关闭或刷新此页面后无法再次查看，只能撤销并重新创建。</p>
              <div className="mt-3 flex gap-2">
                <code className="min-w-0 flex-1 overflow-x-auto rounded-lg bg-bg px-3 py-2 text-xs">{newKey}</code>
                <button onClick={copyKey} type="button" className="flex h-9 shrink-0 items-center gap-1.5 rounded-lg border border-border-light bg-surface px-3 text-xs font-medium hover:bg-surface-hover">
                  {copied ? <Check className="h-4 w-4 text-success" /> : <Copy className="h-4 w-4" />} {copied ? "已复制" : "复制"}
                </button>
              </div>
            </div>
          )}
        </AdminSection>

        <section className="overflow-hidden rounded-2xl border border-border-light bg-surface">
          <div className="flex min-h-14 items-center justify-between gap-2 border-b border-border-light px-5 py-3">
            <h2 className="font-semibold">已创建密钥</h2>
            <InfoButton onClick={() => setHelpItem(HELP.keys)} label="密钥列表说明" />
          </div>
          {items.length === 0 ? (
            <p className="px-5 py-8 text-center text-sm text-muted">尚未创建 Agent API Key</p>
          ) : (
            <div className="divide-y divide-border-light">
              {items.map((item) => (
                <div key={item.id} className="flex flex-col gap-3 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-sm font-medium">{item.name}</span>
                      <span className={`rounded-full px-2 py-0.5 text-[11px] ${item.revoked_at ? "bg-surface-hover text-muted" : "bg-success/10 text-success"}`}>{item.revoked_at ? "已撤销" : "有效"}</span>
                    </div>
                    <code className="mt-1 block text-xs text-muted">{item.key_prefix}••••••••{item.key_suffix}</code>
                    <p className="mt-1 text-[11px] text-muted">创建：{formatTime(item.created_at)} · 最近使用：{formatTime(item.last_used_at)}</p>
                  </div>
                  {!item.revoked_at && (
                    <button onClick={() => void revokeKey(item)} className="flex h-9 shrink-0 items-center justify-center gap-1.5 rounded-lg border border-warning/30 px-3 text-xs text-fg-secondary hover:bg-warning/10">
                      <Trash2 className="h-3.5 w-3.5" /> 撤销
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </section>

        <AdminSection title="修改管理员密码" icon={<LockKeyhole className="h-5 w-5 text-accent" />}
          info={<InfoButton onClick={() => setHelpItem(HELP.password)} label="修改密码说明" />}>
          <p className="mb-4 text-xs text-muted">修改成功后会撤销该账号的全部浏览器登录令牌，需要重新登录。</p>
          <form onSubmit={changePassword} className="grid gap-3 sm:grid-cols-3">
            <input type="password" autoComplete="current-password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} required className="h-10 rounded-lg border border-border-light bg-bg px-3 text-sm outline-none focus:border-accent/50" placeholder="当前密码" />
            <input type="password" autoComplete="new-password" minLength={8} value={nextPassword} onChange={(event) => setNextPassword(event.target.value)} required className="h-10 rounded-lg border border-border-light bg-bg px-3 text-sm outline-none focus:border-accent/50" placeholder="新密码（至少 8 位）" />
            <input type="password" autoComplete="new-password" minLength={8} value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} required className="h-10 rounded-lg border border-border-light bg-bg px-3 text-sm outline-none focus:border-accent/50" placeholder="再次输入新密码" />
            <button disabled={changingPassword} className="flex h-10 items-center justify-center gap-1.5 rounded-lg border border-border-light px-4 text-sm font-medium hover:bg-surface-hover disabled:opacity-60 sm:col-start-3">
              {changingPassword && <Loader2 className="h-4 w-4 animate-spin" />} 修改密码
            </button>
          </form>
        </AdminSection>
      </div>
      <HelpModal item={helpItem} onClose={() => setHelpItem(null)} />
    </main>
  );
}
