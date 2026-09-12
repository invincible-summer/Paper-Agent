"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Database,
  HardDrive,
  Loader2,
  ShieldCheck,
  Trash2,
  UserRound,
  KeyRound,
  AlertTriangle,
} from "lucide-react";

import {
  AdminApiError,
  cleanupAccount,
  listAccountStorage,
  type AccountStorageItem,
} from "@/lib/admin-api";
import {
  AdminHeader, ConfirmModal, HelpModal, InfoButton, type HelpEntry,
} from "@/components/admin/AdminUI";
import { useAuthStore } from "@/stores/auth";

const HELP: Record<string, HelpEntry> = {
  accountDelete: {
    title: "彻底删除账号数据",
    entries: [
      ["删除范围", "该账号的历史会话、上传文件、Trace 记录、会话向量；API Key 还会删除其 API 会话 / Checkpoint / 私有文件，并撤销该密钥。"],
      ["不可恢复", "删除立即执行且无法撤销；账号本身（登录凭据）不会被删除。"],
      ["判断依据", "「历史 / 上传 / Trace / API 私有」各列显示了将被清除的内容规模，删除前请核对。"],
    ],
  },
};

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
  const [helpItem, setHelpItem] = useState<HelpEntry | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<AccountStorageItem | null>(null);

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
    setBusy(item.account_id);
    setError("");
    setNotice("");
    try {
      const result = await cleanupAccount(item.account_type, item.account_id);
      setNotice(`已不可恢复地清除“${item.label || item.account_id}”的数据：${result.files} 个文件 / ${bytes(result.bytes)}`);
      await refresh();
    } catch (err) {
      handleApiError(err);
    } finally {
      setBusy(null);
    }
  };

  if (!checked || (user?.role === "administrator" && loading)) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-accent" />
      </div>
    );
  }

  if (!user || user.role !== "administrator") {
    return (
      <div className="flex min-h-[60vh] items-center justify-center px-4">
        <div className="max-w-md rounded-[9px] border border-border-light bg-surface p-8 text-center shadow-sm">
          <ShieldCheck className="mx-auto mb-3 h-10 w-10 text-muted" />
          <h1 className="text-lg font-semibold text-fg">无权访问管理页面</h1>
          <p className="mt-2 text-[13px] text-muted">仅管理员账号可以清理各账号数据。</p>
          <button onClick={() => router.replace("/chat")}
            className="mt-5 rounded-[7px] bg-accent px-4 py-2 text-white">返回对话</button>
        </div>
      </div>
    );
  }

  const totalOwned = (totals.history_bytes || 0) + (totals.upload_bytes || 0)
    + (totals.trace_bytes || 0) + (totals.api_private_bytes || 0);

  return (
    <>
      <div className="space-y-6">
        <AdminHeader title="账号数据清理" icon={<Trash2 className="h-5 w-5" />}
          subtitle="统一查看并不可恢复地清除各账号的历史、上传文件、Trace 与 API 私有数据。"
          onRefresh={() => void refresh()} refreshing={loading} />

        {error && <div className="rounded-[7px] border border-error/40 bg-error/10 p-3 text-[13px] text-error">{error}</div>}
        {notice && <div className="rounded-[7px] border border-success/40 bg-success/10 p-3 text-[13px] text-success">{notice}</div>}

        <section className="grid gap-4 md:grid-cols-2">
          <div className="rounded-[7px] border border-border-light bg-surface p-4">
            <Database className="mb-2 h-5 w-5 text-accent" />
            <div className="tnum text-2xl font-bold">{items.length}</div>
            <div className="text-[13px] text-muted">可管理的账号 / Agent Key</div>
          </div>
          <div className="rounded-[7px] border border-border-light bg-surface p-4">
            <HardDrive className="mb-2 h-5 w-5 text-accent" />
            <div className="tnum text-2xl font-bold">{bytes(totalOwned)}</div>
            <div className="text-[13px] text-muted">账号私有文件与历史占用</div>
          </div>
        </section>

        <section className="overflow-hidden rounded-[7px] border border-border-light bg-surface">
          <div className="flex min-h-11 items-center justify-between gap-2 border-b border-border-light px-4 py-2.5">
            <h2 className="font-semibold">账号清单</h2>
            <InfoButton onClick={() => setHelpItem(HELP.accountDelete)} label="账号数据删除说明" />
          </div>
          <div className="max-h-[65vh] overflow-auto">
            <table className="w-full text-left text-[13px]">
              <thead className="sticky top-0 bg-surface text-[12px] uppercase text-muted">
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
                            <div className="text-[12px] text-muted">
                              {item.account_type === "web_user" ? item.role : `Agent Key ${item.account_id.slice(0, 8)}`}
                              {item.disabled ? " · 已停用/撤销" : ""}
                            </div>
                          </div>
                        </div>
                      </td>
                  <td className="tnum px-3 py-2 text-muted">
                    {item.history_count} 个 · {bytes(item.history_bytes)}
                    <div className="text-[12px] text-muted">{item.paper_count} 篇论文记录</div>
                  </td>
                  <td className="tnum px-3 py-2 text-muted">
                    {item.upload_count} 个 · {bytes(item.upload_bytes)}
                    <div className="text-[12px] text-muted">{item.attachment_count} 个附件引用</div>
                  </td>
                  <td className="tnum px-3 py-2 text-muted">
                    {item.trace_count} 个 · {bytes(item.trace_bytes)}
                  </td>
                  <td className="tnum px-3 py-2 text-muted">
                    {item.account_type === "api_key"
                      ? `${item.api_sessions || 0} 会话 · ${bytes(item.api_private_bytes || 0)} · ${item.api_private_artifacts || 0} 文件`
                      : "—"}
                  </td>
                  <td className="px-3 py-2">
                    <button onClick={() => setConfirmDelete(item)}
                      disabled={busy !== null}
                      className="flex items-center gap-1 rounded-[7px] border border-error/40 px-2.5 py-1.5 text-[12px] text-error hover:bg-error/10 disabled:opacity-50">
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

      {confirmDelete && (
        <ConfirmModal title={`彻底删除「${confirmDelete.label || confirmDelete.account_id}」`} danger
          confirmLabel="确认删除" busy={busy === confirmDelete.account_id}
          body={"将不可恢复地清除：历史会话、上传文件、Trace 记录、会话向量"
            + (confirmDelete.account_type === "api_key" ? "、API 会话/Checkpoint/私有文件，并撤销该 Agent Key" : "")
            + "。此操作无法撤销。"}
          onConfirm={() => { const item = confirmDelete; setConfirmDelete(null); void deleteAccount(item); }}
          onClose={() => setConfirmDelete(null)} />
      )}
      <HelpModal item={helpItem} onClose={() => setHelpItem(null)} />
    </>
  );
}
