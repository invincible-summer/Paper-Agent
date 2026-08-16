"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  ArrowLeft,
  Database,
  HardDrive,
  Loader2,
  RefreshCw,
  ShieldCheck,
  Trash2,
  UserRound,
  KeyRound,
  AlertTriangle,
} from "lucide-react";

import {
  AdminApiError,
  cleanupAccount,
  cleanupWebPaperCache,
  listAccountStorage,
  type AccountStorageItem,
} from "@/lib/admin-api";
import { useAuthStore } from "@/stores/auth";

function bytes(value: number): string {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = value;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

export default function AccountsDataAdminPage() {
  const router = useRouter();
  const checked = useAuthStore((s) => s.checked);
  const user = useAuthStore((s) => s.user);
  const signOut = useAuthStore((s) => s.signOut);
  const [items, setItems] = useState<AccountStorageItem[]>([]);
  const [totals, setTotals] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

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
      const data = await listAccountStorage();
      setItems(data.items);
      setTotals(data.totals);
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

  const deleteAccount = async (item: AccountStorageItem) => {
    const label = item.label || item.account_id;
    const confirmed = window.confirm(
      `确定彻底删除“${label}”的全部数据吗？\n\n` +
      `将不可恢复地清除：历史会话、上传文件、Trace 记录、会话向量` +
      (item.account_type === "api_key" ? "、API 会话/Checkpoint/私有文件，并撤销该 Agent Key" : "") +
      `。\n\n此操作无法撤销。`
    );
    if (!confirmed) return;
    setBusy(item.account_id);
    setError("");
    setNotice("");
    try {
      const result = await cleanupAccount(item.account_type, item.account_id);
      setNotice(`已不可恢复地清除“${label}”的数据：${result.files} 个文件 / ${bytes(result.bytes)}`);
      await refresh();
    } catch (err) {
      handleApiError(err);
    } finally {
      setBusy(null);
    }
  };

  const cleanPaperCache = async () => {
    const confirmed = window.confirm(
      "确定清理全部 Web 拉取论文缓存吗？\n\n" +
      "将不可恢复地删除 data/pdfs 与 data/assets 中的论文 PDF 和图表资产。\n" +
      "这不删除账号历史；后续深读某篇论文时会自动重新下载。"
    );
    if (!confirmed) return;
    setBusy("paper-cache");
    setError("");
    setNotice("");
    try {
      const result = await cleanupWebPaperCache();
      setNotice(`论文缓存已清理：${result.files} 个文件 / ${bytes(result.bytes)}`);
      await refresh();
    } catch (err) {
      handleApiError(err);
    } finally {
      setBusy(null);
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
          <p className="mt-2 text-sm text-muted">仅管理员账号可以清理各账号数据。</p>
          <button onClick={() => router.replace("/chat")}
            className="mt-5 rounded-lg bg-accent px-4 py-2 text-white">返回对话</button>
        </div>
      </div>
    );
  }

  const totalOwned = (totals.history_bytes || 0) + (totals.upload_bytes || 0)
    + (totals.trace_bytes || 0) + (totals.api_private_bytes || 0);

  return (
    <main className="min-h-screen bg-bg px-4 py-8 text-fg sm:px-8">
      <div className="mx-auto max-w-6xl space-y-6">
        <header className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <button onClick={() => router.push("/chat")}
              className="rounded-lg border border-border-light p-2">
              <ArrowLeft className="h-4 w-4" />
            </button>
            <div>
              <h1 className="text-2xl font-bold">账号数据清理</h1>
              <p className="text-sm text-muted">统一查看并不可恢复地清除各账号的历史、上传文件、Trace 与 API 私有数据。</p>
            </div>
          </div>
          <div className="flex gap-2">
            <button onClick={() => router.push("/admin/api-storage")}
              className="rounded-lg border border-border-light px-3 py-2 text-sm">API 存储策略</button>
            <button onClick={() => router.push("/admin/agent-keys")}
              className="rounded-lg border border-border-light px-3 py-2 text-sm">Agent Keys</button>
            <button onClick={() => void refresh()} disabled={busy !== null}
              className="rounded-lg border border-border-light p-2">
              <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            </button>
          </div>
        </header>

        {error && <div className="rounded-lg border border-red-400/40 bg-red-500/10 p-3 text-sm text-red-500">{error}</div>}
        {notice && <div className="rounded-lg border border-success/40 bg-success/10 p-3 text-sm text-success">{notice}</div>}

        <section className="grid gap-4 md:grid-cols-3">
          <div className="rounded-xl border border-border-light bg-surface p-4">
            <Database className="mb-2 h-5 w-5 text-accent" />
            <div className="text-2xl font-bold">{items.length}</div>
            <div className="text-sm text-muted">可管理的账号 / Agent Key</div>
          </div>
          <div className="rounded-xl border border-border-light bg-surface p-4">
            <HardDrive className="mb-2 h-5 w-5 text-accent" />
            <div className="text-2xl font-bold">{bytes(totalOwned)}</div>
            <div className="text-sm text-muted">账号私有文件与历史占用</div>
          </div>
          <button onClick={() => void cleanPaperCache()} disabled={busy !== null}
            className="rounded-xl border border-amber-500 bg-amber-500/10 p-4 text-left">
            <Trash2 className="mb-2 h-5 w-5 text-amber-600" />
            <div className="text-lg font-bold">清理拉取论文缓存</div>
            <div className="text-sm text-muted">不可恢复删除 web data/pdfs 与 data/assets（深读时会重新下载）</div>
          </button>
        </section>

        <section className="overflow-hidden rounded-xl border border-border-light bg-surface">
          <div className="border-b border-border-light px-4 py-3">
            <h2 className="font-semibold">账号清单</h2>
          </div>
          <div className="max-h-[65vh] overflow-auto">
            <table className="w-full text-left text-sm">
              <thead className="sticky top-0 bg-surface text-[11px] uppercase text-muted">
                <tr>
                  <th className="px-4 py-2">账号</th>
                  <th className="px-3 py-2">历史</th>
                  <th className="px-3 py-2">上传</th>
                  <th className="px-3 py-2">Trace</th>
                  <th className="px-3 py-2">API 私有</th>
                  <th className="px-3 py-2">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border-light">
                {items.map((item) => {
                  return (
                    <tr key={`${item.account_type}-${item.account_id}`}>
                      <td className="px-4 py-2">
                        <div className="flex items-center gap-2">
                          {item.account_type === "web_user"
                            ? <UserRound className="h-4 w-4 text-accent" />
                            : <KeyRound className="h-4 w-4 text-accent2" />}
                          <div className="min-w-0">
                            <div className="truncate font-medium">{item.label}</div>
                            <div className="text-[11px] text-muted">
                              {item.account_type === "web_user" ? item.role : `Agent Key ${item.account_id.slice(0, 8)}`}
                              {item.disabled ? " · 已停用/撤销" : ""}
                            </div>
                          </div>
                        </div>
                      </td>
                      <td className="px-3 py-2 text-muted">
                        {item.history_count} 个 · {bytes(item.history_bytes)}
                        <div className="text-[11px] text-muted/70">{item.paper_count} 篇论文记录</div>
                      </td>
                      <td className="px-3 py-2 text-muted">
                        {item.upload_count} 个 · {bytes(item.upload_bytes)}
                        <div className="text-[11px] text-muted/70">{item.attachment_count} 个附件引用</div>
                      </td>
                      <td className="px-3 py-2 text-muted">
                        {item.trace_count} 个 · {bytes(item.trace_bytes)}
                      </td>
                      <td className="px-3 py-2 text-muted">
                        {item.account_type === "api_key"
                          ? `${item.api_sessions || 0} 会话 · ${bytes(item.api_private_bytes || 0)} · ${item.api_private_artifacts || 0} 文件`
                          : "—"}
                      </td>
                      <td className="px-3 py-2">
                        <button onClick={() => void deleteAccount(item)}
                          disabled={busy !== null}
                          className="flex items-center gap-1 rounded-lg border border-error/40 px-2.5 py-1.5 text-xs text-error hover:bg-error/10 disabled:opacity-50">
                          {busy === item.account_id
                            ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            : <Trash2 className="h-3.5 w-3.5" />}
                          {busy === item.account_id ? "清理中…" : "彻底删除"}
                        </button>
                      </td>
                    </tr>
                  );
                })}
                {!items.length && (
                  <tr><td colSpan={6} className="px-4 py-10 text-center text-muted">
                    <AlertTriangle className="mx-auto mb-2 h-6 w-6" />暂无可管理的账号数据
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </main>
  );
}
