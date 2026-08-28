"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  CheckCircle2, CircleDashed, Loader2, MessageSquare, RotateCcw, Trash2,
} from "lucide-react";
import {
  AdminHeader, ConfirmModal,
} from "@/components/admin/AdminUI";
import {
  AdminApiError, deleteFeedback, listFeedback, updateFeedbackStatus,
  type FeedbackItem, type FeedbackListResponse,
} from "@/lib/admin-api";
import { useAuthStore } from "@/stores/auth";

type StatusFilter = "all" | "open" | "resolved";

const ROLE_LABELS: Record<string, string> = {
  administrator: "管理员", user: "用户", guest: "游客", local: "本地",
};

const CATEGORY_STYLES: Record<string, string> = {
  问题报告: "border-warning/40 bg-warning/10 text-warning",
  功能建议: "border-accent/40 bg-accent-soft/70 text-accent",
  其他: "border-border-light bg-surface-hover text-fg-secondary",
};

function formatTime(timestamp: number): string {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium", timeStyle: "short",
  }).format(new Date(timestamp * 1000));
}

export default function FeedbackAdminPage() {
  const router = useRouter();
  const checked = useAuthStore((s) => s.checked);
  const user = useAuthStore((s) => s.user);
  const signOut = useAuthStore((s) => s.signOut);
  const [data, setData] = useState<FeedbackListResponse | null>(null);
  const [filter, setFilter] = useState<StatusFilter>("all");
  const [loading, setLoading] = useState(true);
  const [workingId, setWorkingId] = useState("");
  const [error, setError] = useState("");
  const [pendingDelete, setPendingDelete] = useState<FeedbackItem | null>(null);

  useEffect(() => { void useAuthStore.getState().hydrate(); }, []);

  const handleError = useCallback((err: unknown) => {
    if (err instanceof AdminApiError && err.status === 401) {
      signOut(); router.replace("/login"); return;
    }
    setError(err instanceof Error ? err.message : "请求失败");
  }, [router, signOut]);

  const refresh = useCallback(async (status: StatusFilter) => {
    setLoading(true); setError("");
    try {
      setData(await listFeedback(status === "all" ? undefined : status));
    } catch (err) { handleError(err); } finally { setLoading(false); }
  }, [handleError]);

  useEffect(() => {
    if (!checked) return;
    if (!user) { router.replace("/login"); return; }
    if (user.role !== "administrator") { setLoading(false); return; }
    void refresh(filter);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [checked, user, router]);

  const applyFilter = (next: StatusFilter) => {
    setFilter(next);
    void refresh(next);
  };

  const toggleStatus = async (item: FeedbackItem) => {
    if (workingId) return;
    setWorkingId(item.id); setError("");
    try {
      await updateFeedbackStatus(item.id, item.status === "open");
      await refresh(filter);
    } catch (err) { handleError(err); } finally { setWorkingId(""); }
  };

  const confirmDelete = async () => {
    if (!pendingDelete || workingId) return;
    setWorkingId(pendingDelete.id); setError("");
    try {
      await deleteFeedback(pendingDelete.id);
      setPendingDelete(null);
      await refresh(filter);
    } catch (err) { handleError(err); } finally { setWorkingId(""); }
  };

  if (!checked || (loading && !data)) {
    return <div className="flex min-h-[60vh] items-center justify-center"><Loader2 className="h-6 w-6 animate-spin text-accent" /></div>;
  }
  if (!user || user.role !== "administrator") {
    return <div className="py-16 text-center text-muted">仅管理员可访问。</div>;
  }

  const counts = data?.counts;
  const items = data?.items ?? [];
  const filterTabs: Array<{ key: StatusFilter; label: string; count: number }> = [
    { key: "all", label: "全部", count: counts?.total ?? 0 },
    { key: "open", label: "待处理", count: counts?.open ?? 0 },
    { key: "resolved", label: "已处理", count: counts?.resolved ?? 0 },
  ];

  return <>
    <div className="space-y-5 pb-8">
      <AdminHeader title="用户反馈" icon={<MessageSquare className="h-5 w-5" />}
        subtitle="用户从「反馈」页面提交的问题报告与功能建议；仅管理员可见。"
        onRefresh={() => void refresh(filter)} refreshing={loading} />

      {error && <div className="rounded-lg border border-error/40 bg-error/10 p-3 text-sm text-error">{error}</div>}

      <div className="flex flex-wrap gap-2">
        {filterTabs.map(({ key, label, count }) => (
          <button key={key} type="button" onClick={() => applyFilter(key)}
            aria-pressed={filter === key}
            className={`flex h-8 items-center gap-1.5 rounded-full border px-3.5 text-xs transition-colors ${
              filter === key
                ? "border-accent bg-accent-soft/70 font-medium text-accent"
                : "border-border-light text-fg-secondary hover:bg-surface-hover hover:text-fg"
            }`}>
            {key === "open" && <CircleDashed className="h-3.5 w-3.5" />}
            {key === "resolved" && <CheckCircle2 className="h-3.5 w-3.5" />}
            {label}
            <span className="tnum opacity-70">{count}</span>
          </button>
        ))}
      </div>

      {items.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-border-light bg-surface p-10 text-center text-sm text-muted">
          {filter === "all" ? "还没有收到任何反馈" : filter === "open" ? "没有待处理的反馈" : "没有已处理的反馈"}
        </div>
      ) : (
        <ul className="space-y-3">
          {items.map((item) => (
            <li key={item.id}
              className={`rounded-2xl border bg-surface p-4 transition-colors ${
                item.status === "resolved" ? "border-border-light opacity-75" : "border-accent/25"
              }`}>
              <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
                <span className={`rounded-full border px-2 py-0.5 ${
                  CATEGORY_STYLES[item.category] ?? CATEGORY_STYLES["其他"]
                }`}>{item.category}</span>
                {item.status === "resolved" ? (
                  <span className="flex items-center gap-1 rounded-full border border-success/40 bg-success/10 px-2 py-0.5 text-success">
                    <CheckCircle2 className="h-3 w-3" />已处理
                  </span>
                ) : (
                  <span className="flex items-center gap-1 rounded-full border border-warning/40 bg-warning/10 px-2 py-0.5 text-warning">
                    <CircleDashed className="h-3 w-3" />待处理
                  </span>
                )}
                <span className="text-muted">
                  {item.username || "匿名"} · {ROLE_LABELS[item.role] ?? item.role} · {formatTime(item.created_at)}
                </span>
              </div>
              <p className="whitespace-pre-wrap text-sm leading-6 text-fg">{item.content}</p>
              {item.contact && (
                <p className="mt-2 text-xs text-muted">联系方式：<span className="text-fg-secondary">{item.contact}</span></p>
              )}
              <div className="mt-3 flex flex-wrap justify-end gap-2">
                <button type="button" onClick={() => void toggleStatus(item)} disabled={workingId === item.id}
                  className="flex h-8 items-center gap-1.5 rounded-lg border border-border-light px-3 text-xs text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg disabled:opacity-50">
                  {workingId === item.id
                    ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    : item.status === "open"
                      ? <CheckCircle2 className="h-3.5 w-3.5" />
                      : <RotateCcw className="h-3.5 w-3.5" />}
                  {item.status === "open" ? "标记已处理" : "重新打开"}
                </button>
                <button type="button" onClick={() => setPendingDelete(item)}
                  className="flex h-8 items-center gap-1.5 rounded-lg border border-error/30 px-3 text-xs text-error transition-colors hover:bg-error/10 disabled:opacity-50">
                  <Trash2 className="h-3.5 w-3.5" />
                  删除
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>

    {pendingDelete && (
      <ConfirmModal
        title="删除这条反馈？"
        body={`将永久删除「${pendingDelete.username || "匿名"}」于 ${formatTime(pendingDelete.created_at)} 提交的反馈，此操作不可恢复。`}
        confirmLabel="删除"
        danger
        busy={workingId === pendingDelete.id}
        onConfirm={() => void confirmDelete()}
        onClose={() => { if (workingId !== pendingDelete.id) setPendingDelete(null); }}
      />
    )}
  </>;
}
