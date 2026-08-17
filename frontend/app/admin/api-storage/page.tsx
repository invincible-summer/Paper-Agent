"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { AlertTriangle, Database, HelpCircle, Loader2, ShieldCheck, Trash2 } from "lucide-react";
import {
  AdminApiError, executeApiStorageCleanup, executeApiStorageLegacyScan,
  getApiStorageCleanupRuns, getApiStoragePolicy, getApiStorageStatus,
  getApiStorageUsage, previewApiStorageAction, updateApiStoragePolicy,
  type ApiStoragePolicy, type StorageHelpItem,
} from "@/lib/admin-api";
import { AdminHeader } from "@/components/admin/AdminUI";
import { useAuthStore } from "@/stores/auth";

const PRESETS: Record<string, Partial<ApiStoragePolicy>> = {
  privacy: { preset: "privacy", session_ttl_seconds: 7200, upload_ttl_seconds: 7200, export_ttl_seconds: 7200, public_pdf_ttl_seconds: 259200, cache_ttl_seconds: 2592000, trace_mode: "off" },
  balanced: { preset: "balanced", session_ttl_seconds: 604800, upload_ttl_seconds: 604800, export_ttl_seconds: 86400, public_pdf_ttl_seconds: 259200, cache_ttl_seconds: 7776000, trace_mode: "off" },
  performance: { preset: "performance", session_ttl_seconds: 2592000, upload_ttl_seconds: 2592000, export_ttl_seconds: 604800, public_pdf_ttl_seconds: 259200, cache_ttl_seconds: 15552000, trace_mode: "metadata", trace_ttl_seconds: 604800 },
};

function bytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  return `${(value / 1024 ** 3).toFixed(2)} GiB`;
}

function InfoButton({ onClick }: { onClick: () => void }) {
  return <button type="button" onClick={onClick} className="rounded p-1 text-muted hover:bg-surface-hover hover:text-accent" aria-label="查看详细说明"><HelpCircle className="h-4 w-4" /></button>;
}

export default function ApiStorageAdminPage() {
  const router = useRouter();
  const checked = useAuthStore((s) => s.checked);
  const user = useAuthStore((s) => s.user);
  const signOut = useAuthStore((s) => s.signOut);
  const [policy, setPolicy] = useState<ApiStoragePolicy | null>(null);
  const [draft, setDraft] = useState<ApiStoragePolicy | null>(null);
  const [help, setHelp] = useState<Record<string, StorageHelpItem>>({});
  const [usage, setUsage] = useState<{ categories: Array<{ category: string; status: string; count: number; bytes: number }>; total_bytes: number } | null>(null);
  const [status, setStatus] = useState<{ heavy_writes_paused: boolean; disk_percent: number | null; pause_reason: string | null } | null>(null);
  const [runs, setRuns] = useState<Array<Record<string, string | number | null>>>([]);
  const [helpItem, setHelpItem] = useState<StorageHelpItem | null>(null);
  const [confirm, setConfirm] = useState<{ title: string; summary: string; execute: () => Promise<void> } | null>(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => { void useAuthStore.getState().hydrate(); }, []);
  const handleError = useCallback((err: unknown) => {
    if (err instanceof AdminApiError && err.status === 401) { signOut(); router.replace("/login"); return; }
    setError(err instanceof Error ? err.message : "请求失败");
  }, [router, signOut]);

  const refresh = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const [p, u, s, r] = await Promise.all([getApiStoragePolicy(), getApiStorageUsage(), getApiStorageStatus(), getApiStorageCleanupRuns()]);
      setPolicy(p.policy); setDraft(p.policy); setHelp(p.help.items); setUsage(u); setStatus(s); setRuns(r.items);
    } catch (err) { handleError(err); } finally { setLoading(false); }
  }, [handleError]);

  useEffect(() => {
    if (!checked) return;
    if (!user) { router.replace("/login"); return; }
    if (user.role !== "administrator") { setLoading(false); return; }
    void refresh();
  }, [checked, user, router, refresh]);

  const changes = useMemo(() => {
    if (!policy || !draft) return {};
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(draft) as Array<keyof ApiStoragePolicy>) {
      if (["version", "updated_by", "updated_at"].includes(key)) continue;
      if (draft[key] !== policy[key]) out[key] = draft[key];
    }
    return out;
  }, [policy, draft]);

  const save = async () => {
    if (!policy || !Object.keys(changes).length) return;
    setWorking(true); setError("");
    try {
      const result = await updateApiStoragePolicy(policy.version, changes);
      setPolicy(result.policy); setDraft(result.policy);
    } catch (err) {
      if (err instanceof AdminApiError && err.status === 409) {
        try {
          const preview = await previewApiStorageAction("policy_update", changes);
          setConfirm({
            title: "确认危险策略修改",
            summary: `该修改可能缩短现有数据保留期、启用 Full Trace 或允许紧急删除。确认令牌 10 分钟内有效且只能使用一次。预估可处理 ${String(preview.preview.deleted_artifacts ?? 0)} 个过期文件。`,
            execute: async () => {
              const result = await updateApiStoragePolicy(policy.version, changes, preview.token);
              setPolicy(result.policy); setDraft(result.policy);
            },
          });
        } catch (previewError) { handleError(previewError); }
      } else handleError(err);
    } finally { setWorking(false); }
  };

  const requestAction = async (action: "immediate_cleanup" | "legacy_scan") => {
    setWorking(true); setError("");
    try {
      const preview = await previewApiStorageAction(action);
      setConfirm({
        title: action === "legacy_scan" ? "确认扫描 API 遗留文件" : "确认立即清理 API 数据",
        summary: `仅处理 data/openai_api；不会读取或删除自制前端历史和文件。预计处理 ${String(preview.preview.deleted_artifacts ?? 0)} 个过期 artifact，约 ${bytes(Number(preview.preview.reclaimed_bytes ?? 0))}。操作不可撤销。`,
        execute: async () => {
          if (action === "legacy_scan") await executeApiStorageLegacyScan(preview.token);
          else await executeApiStorageCleanup(preview.token);
          await refresh();
        },
      });
    } catch (err) { handleError(err); } finally { setWorking(false); }
  };

  if (!checked || loading) return <div className="flex min-h-screen items-center justify-center"><Loader2 className="h-6 w-6 animate-spin text-accent" /></div>;
  if (!user || user.role !== "administrator") return <div className="p-8 text-center text-muted">仅管理员可访问。</div>;
  if (!draft || !policy) return <div className="p-8 text-center text-red-500">{error || "策略加载失败"}</div>;

  const show = (key: string) => setHelpItem(help[key]);
  return <main className="min-h-screen bg-bg px-4 py-8 text-fg sm:px-8">
    <div className="mx-auto max-w-6xl space-y-6">
      <AdminHeader title="OpenAI API 存储管理" icon={<Database className="h-5 w-5" />}
        subtitle="仅管理清小搭 /v1 数据，不影响自制前端持久化。"
        current="/admin/api-storage" onRefresh={() => void refresh()} refreshing={loading} />
      {error && <div className="rounded-lg border border-red-400/40 bg-red-500/10 p-3 text-sm text-red-500">{error}</div>}
      <section className="grid gap-4 md:grid-cols-3">
        <div className="rounded-xl border border-border-light bg-surface p-4"><Database className="mb-2 h-5 w-5 text-accent" /><div className="text-2xl font-bold">{bytes(usage?.total_bytes ?? 0)}</div><div className="text-sm text-muted">API artifact 逻辑容量</div></div>
        <div className="rounded-xl border border-border-light bg-surface p-4"><ShieldCheck className="mb-2 h-5 w-5 text-accent" /><div className="text-2xl font-bold">{status?.disk_percent?.toFixed(1) ?? "--"}%</div><div className="text-sm text-muted">服务器磁盘占用</div></div>
        <div className={`rounded-xl border p-4 ${status?.heavy_writes_paused ? "border-amber-500 bg-amber-500/10" : "border-border-light bg-surface"}`}><AlertTriangle className="mb-2 h-5 w-5 text-amber-500" /><div className="text-lg font-bold">{status?.heavy_writes_paused ? "文件重任务已暂停" : "文件重任务正常"}</div><div className="text-sm text-muted">普通文字问答始终可用</div></div>
      </section>
      <section className="rounded-xl border border-border-light bg-surface p-5"><div className="mb-4 flex items-center justify-between"><h2 className="font-semibold">策略预设</h2><InfoButton onClick={() => show(`preset.${draft.preset === "custom" ? "balanced" : draft.preset}`)} /></div><div className="grid gap-3 sm:grid-cols-3">{(["privacy", "balanced", "performance"] as const).map((name) => <button key={name} onClick={() => setDraft({ ...draft, ...PRESETS[name] })} className={`rounded-lg border p-4 text-left ${draft.preset === name ? "border-accent bg-accent/10" : "border-border-light"}`}><div className="font-medium">{{ privacy: "隐私优先", balanced: "均衡", performance: "性能优先" }[name]}</div><div className="mt-1 text-xs text-muted">{help[`preset.${name}`]?.does}</div></button>)}</div></section>
      <section className="grid gap-4 lg:grid-cols-2">
        <div className="rounded-xl border border-border-light bg-surface p-5"><div className="mb-3 flex items-center justify-between"><h2 className="font-semibold">关键保留期</h2><InfoButton onClick={() => show("ttl")} /></div>{([['session_ttl_seconds','会话 Checkpoint'],['upload_ttl_seconds','私有上传'],['export_ttl_seconds','导出'],['public_pdf_ttl_seconds','公共 PDF'],['cache_ttl_seconds','语义/视觉缓存']] as const).map(([key,label]) => <label key={key} className="mb-3 flex items-center justify-between gap-3 text-sm"><span>{label}</span><input type="number" min={1} value={Math.round(draft[key] / 3600)} onChange={(e) => setDraft({ ...draft, preset: "custom", [key]: Number(e.target.value) * 3600 })} className="w-28 rounded border border-border-light bg-bg px-2 py-1 text-right" /><span className="w-8 text-muted">小时</span></label>)}</div>
        <div className="rounded-xl border border-border-light bg-surface p-5"><div className="mb-3 flex items-center justify-between"><h2 className="font-semibold">磁盘阈值</h2><InfoButton onClick={() => show("thresholds")} /></div>{([['observe_threshold_percent','观察'],['pressure_threshold_percent','持续清理'],['critical_threshold_percent','关键'],['hard_stop_threshold_percent','强制保护']] as const).map(([key,label]) => <label key={key} className="mb-3 flex items-center justify-between text-sm"><span>{label}</span><input type="number" disabled={key === "hard_stop_threshold_percent"} value={draft[key]} onChange={(e) => setDraft({ ...draft, preset: "custom", [key]: Number(e.target.value) })} className="w-24 rounded border border-border-light bg-bg px-2 py-1 text-right disabled:opacity-60" /></label>)}</div>
        <div className="rounded-xl border border-border-light bg-surface p-5"><div className="mb-3 flex items-center justify-between"><h2 className="font-semibold">95% 策略</h2><InfoButton onClick={() => show(`critical.${draft.critical_strategy}`)} /></div><select value={draft.critical_strategy} onChange={(e) => setDraft({ ...draft, preset: "custom", critical_strategy: e.target.value as ApiStoragePolicy["critical_strategy"] })} className="w-full rounded border border-border-light bg-bg p-2"><option value="pause_heavy">暂停文件重任务（推荐）</option><option value="emergency_evict">紧急清理，包括最旧非活跃私有上传</option></select></div>
        <div className="rounded-xl border border-border-light bg-surface p-5"><div className="mb-3 flex items-center justify-between"><h2 className="font-semibold">API Trace</h2><InfoButton onClick={() => show(`trace.${draft.trace_mode}`)} /></div><select value={draft.trace_mode} onChange={(e) => setDraft({ ...draft, preset: "custom", trace_mode: e.target.value as ApiStoragePolicy["trace_mode"] })} className="w-full rounded border border-border-light bg-bg p-2"><option value="off">关闭（推荐）</option><option value="metadata">Metadata，最长 7 天</option><option value="full">Full 脱敏排障，最长 7 天</option></select></div>
      </section>
      <div className="flex flex-wrap gap-3"><button disabled={working || !Object.keys(changes).length} onClick={() => void save()} className="rounded-lg bg-accent px-5 py-2 text-sm font-medium text-white disabled:opacity-50">保存策略</button><button disabled={working} onClick={() => void requestAction("immediate_cleanup")} className="flex items-center gap-2 rounded-lg border border-amber-500 px-4 py-2 text-sm text-amber-600"><Trash2 className="h-4 w-4" />预览并立即清理</button><button disabled={working} onClick={() => void requestAction("legacy_scan")} className="rounded-lg border border-border-light px-4 py-2 text-sm">扫描 API 遗留文件</button><InfoButton onClick={() => show("cleanup")} /></div>
      <section className="rounded-xl border border-border-light bg-surface p-5"><h2 className="mb-3 font-semibold">分类容量</h2><div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{usage?.categories.map((item) => <div key={`${item.category}-${item.status}`} className="rounded border border-border-light p-3 text-sm"><div className="font-medium">{item.category}</div><div className="text-muted">{item.status} · {item.count} 个 · {bytes(item.bytes)}</div></div>)}</div></section>
      <section className="rounded-xl border border-border-light bg-surface p-5"><h2 className="mb-3 font-semibold">最近清理记录</h2><div className="space-y-2 text-sm">{runs.slice(0, 8).map((run) => <div key={String(run.id)} className="flex justify-between rounded border border-border-light p-2"><span>{String(run.mode)} · {String(run.status)}</span><span className="text-muted">释放 {bytes(Number(run.reclaimed_bytes ?? 0))}</span></div>)}{!runs.length && <p className="text-muted">暂无清理记录</p>}</div></section>
    </div>
    {helpItem && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={() => setHelpItem(null)}><div className="max-h-[85vh] w-full max-w-2xl overflow-auto rounded-xl bg-surface p-6" onClick={(e) => e.stopPropagation()}><h2 className="mb-4 text-xl font-bold">{helpItem.title}</h2><dl className="grid gap-3 text-sm">{Object.entries({"功能":helpItem.does,"涉及数据":helpItem.affected,"优点":helpItem.benefits,"缺点":helpItem.drawbacks,"隐私":helpItem.privacy,"磁盘":helpItem.disk,"延迟与模型费用":helpItem.latency_cost,"会话连续性":helpItem.continuity,"生效时间":helpItem.effective,"安全兜底":helpItem.fallback,"恢复默认":helpItem.restore}).map(([k,v]) => <div key={k}><dt className="font-semibold">{k}</dt><dd className="mt-1 text-muted">{v}</dd></div>)}</dl><button onClick={() => setHelpItem(null)} className="mt-5 rounded bg-accent px-4 py-2 text-white">我知道了</button></div></div>}
    {confirm && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"><div className="w-full max-w-lg rounded-xl bg-surface p-6"><AlertTriangle className="mb-3 h-7 w-7 text-amber-500" /><h2 className="text-xl font-bold">{confirm.title}</h2><p className="mt-3 text-sm text-muted">{confirm.summary}</p><div className="mt-5 flex justify-end gap-2"><button onClick={() => setConfirm(null)} className="rounded border border-border-light px-4 py-2">取消</button><button onClick={() => { const action = confirm.execute; setConfirm(null); setWorking(true); void action().then(refresh).catch(handleError).finally(() => setWorking(false)); }} className="rounded bg-red-600 px-4 py-2 text-white">再次确认并执行</button></div></div></div>}
  </main>;
}
