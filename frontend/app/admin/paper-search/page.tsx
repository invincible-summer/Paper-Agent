"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { Activity, CheckCircle2, Loader2, Play, RefreshCw, Search, Settings2, XCircle } from "lucide-react";
import { AdminHeader, AdminSection, AdminToggle, ConfirmModal } from "@/components/admin/AdminUI";
import {
  AdminApiError, getLatestPaperDiagnostics, getPaperSearchPolicy,
  runPaperCapabilityDiagnostics, setPaperSourceCapability, updatePaperSearchPolicy,
  type CapabilityDiagnostic, type PaperCapability, type PaperCapabilityState,
  type PaperSearchPolicy, type PaperSearchPolicyResponse, type PlatformDiagnostic,
} from "@/lib/admin-api";
import { useAuthStore } from "@/stores/auth";

const CAPABILITIES: Array<{ key: PaperCapability; label: string; action: string }> = [
  { key: "search", label: "论文搜索", action: "检测" },
  { key: "abstract", label: "摘要获取", action: "检测" },
];

type Filter = "all" | "ok" | "slow" | "failed" | "disabled" | "not_configured" | "unchecked";
type PendingToggle = { source: string; sourceName: string; capability: PaperCapability; enabled: boolean };

function statusLabel(status?: string | null): string {
  return ({ ok: "正常", slow: "缓慢", failed: "失败", not_applicable: "不适用", not_configured: "配置缺失", empty: "结果为空" } as Record<string, string>)[status || ""] || "未检测";
}
function tone(status?: string | null): string {
  if (status === "ok") return "border-emerald-500/30 bg-emerald-500/10 text-emerald-500";
  if (status === "slow" || status === "empty") return "border-amber-500/30 bg-amber-500/10 text-amber-500";
  if (status === "failed") return "border-red-500/30 bg-red-500/10 text-red-500";
  return "border-border-light bg-surface-hover text-muted";
}
function metricText(capability: PaperCapability, metric?: CapabilityDiagnostic | null, state?: PaperCapabilityState): string {
  if (metric) {
    if (capability === "search" && metric.result_count !== undefined) return `${metric.result_count} 篇 · ${metric.latency_ms ?? "—"} ms`;
    if (capability === "abstract" && metric.abstract_length !== undefined) return `${metric.valid_abstract_count ?? 0} 条 · ${metric.abstract_length} 字 · ${metric.latency_ms ?? "—"} ms`;
    return metric.latency_ms != null ? `${metric.latency_ms} ms` : (metric.message || "—");
  }
  if (state?.last_latency_ms != null) return `${state.last_latency_ms} ms`;
  return "尚无检测指标";
}
function diagnosticMethodLabel(): string { return "官方 API"; }
function configurationLabel(status: string): string {
  return ({ ready: "配置就绪", missing_api_key: "缺少 API Key", missing_contact_email: "缺少联系邮箱", license_not_confirmed: "许可未确认", incomplete_credentials: "凭证不完整", index_empty: "本地索引为空", index_unavailable: "本地索引不可用" } as Record<string, string>)[status] || status;
}
function supportsCapability(source: PaperSearchPolicyResponse["source_catalog"][number], capability: PaperCapability): boolean {
  return capability === "search" ? source.supports_search : source.supports_abstract;
}

export default function PaperSearchAdminPage() {
  const router = useRouter();
  const user = useAuthStore((s) => s.user);
  const [data, setData] = useState<PaperSearchPolicyResponse | null>(null);
  const [diagnostics, setDiagnostics] = useState<Record<string, PlatformDiagnostic>>({});
  const [filter, setFilter] = useState<Filter>("all");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState<PendingToggle | null>(null);

  const load = useCallback(async () => {
    setError("");
    try {
      const [policy, latest] = await Promise.all([getPaperSearchPolicy(), getLatestPaperDiagnostics()]);
      setData(policy);
      const platforms = latest.capability?.platforms || [];
      setDiagnostics(Object.fromEntries(platforms.map((item) => [item.source, item])));
    } catch (err) {
      if (err instanceof AdminApiError && err.status === 403) router.replace("/chat");
      else setError(err instanceof Error ? err.message : "加载失败");
    }
  }, [router]);
  useEffect(() => { void load(); }, [load]);

  const updateGlobalPolicy = async (changes: Partial<Pick<PaperSearchPolicy, "sources" | "search_deadline_seconds" | "per_source_timeout_seconds" | "routing_mode">>) => {
    if (!data) return;
    setBusy(true); setError("");
    try { const result = await updatePaperSearchPolicy(data.policy.version, changes); setData((old) => old ? { ...old, policy: result.policy, capabilities: result.policy.capabilities } : old); setNotice("论文搜索策略已更新"); }
    catch (err) { setError(err instanceof Error ? err.message : "更新失败"); }
    finally { setBusy(false); }
  };
  const runBatch = async (sources?: string[], capability?: "connectivity" | PaperCapability) => {
    setBusy(true); setError("");
    try { const result = await runPaperCapabilityDiagnostics(sources, capability); setDiagnostics(Object.fromEntries((result.platforms || []).map((item) => [item.source, item]))); await load(); setNotice("检测完成"); }
    catch (err) { setError(err instanceof Error ? err.message : "检测失败"); }
    finally { setBusy(false); }
  };
  const confirmToggle = async () => {
    if (!data || !pending) return;
    setBusy(true); setError("");
    try { const result = await setPaperSourceCapability(pending.source, pending.capability, pending.enabled, data.policy.version); setData((old) => old ? { ...old, policy: result.policy, capabilities: result.policy.capabilities } : old); setNotice("平台能力开关已更新"); setPending(null); }
    catch (err) { setError(err instanceof Error ? err.message : "更新失败"); }
    finally { setBusy(false); }
  };
  if (!data) return <div className="p-8 text-sm text-muted">{error || "加载中…"}</div>;

  const catalog = data.source_catalog;
  const visible = catalog.filter((source) => {
    if (filter === "all") return true;
    const states = data.policy.capabilities[source.id];
    const metrics = diagnostics[source.id];
    if (filter === "unchecked") return !metrics;
    if (filter === "disabled") return Object.values(states || {}).some((s) => !s.enabled);
    const statuses = CAPABILITIES.map(({ key }) => metrics?.[key]?.status || states?.[key]?.last_diagnostic_status);
    return statuses.some((status) => status === filter);
  });

  return <div className="mx-auto max-w-7xl p-6">
    <AdminHeader title="论文平台" subtitle="平台只提供论文搜索与有效摘要获取；网络论文全文探测、下载和自动升级已永久下线。" icon={<Search className="h-5 w-5" />} current="/admin/paper-search" />
    {error && <div className="mb-4 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-500">{error}</div>}
    {notice && <div className="mb-4 rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-3 text-sm text-emerald-500">{notice}</div>}
    <AdminSection title="能力矩阵" icon={<Activity className="h-5 w-5 text-accent" />}>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <button onClick={() => void runBatch()} disabled={busy} className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-xs text-white disabled:opacity-50"><Play className="h-3.5 w-3.5" />检测所有平台</button>
        <button onClick={() => void load()} disabled={busy} className="inline-flex items-center gap-1.5 rounded-lg border border-border-light px-3 py-2 text-xs text-fg-secondary disabled:opacity-50"><RefreshCw className="h-3.5 w-3.5" />刷新</button>
        {(["all", "ok", "slow", "failed", "disabled", "unchecked"] as Filter[]).map((item) => <button key={item} onClick={() => setFilter(item)} className={`rounded-lg border px-2.5 py-1.5 text-xs ${filter === item ? "border-accent bg-accent-soft text-accent" : "border-border-light text-muted"}`}>{item === "all" ? "全部" : item === "disabled" ? "已关闭" : item === "unchecked" ? "未检测" : statusLabel(item)}</button>)}
      </div>
      <div className="overflow-x-auto rounded-xl border border-border-light">
        <table className="min-w-[860px] w-full text-left text-xs"><thead className="bg-surface-hover text-muted"><tr><th className="px-3 py-3">平台</th><th className="px-3 py-3">配置</th>{CAPABILITIES.map((item) => <th key={item.key} className="px-3 py-3">{item.label}</th>)}<th className="px-3 py-3">最近检测</th><th className="px-3 py-3">说明</th></tr></thead>
          <tbody>{visible.map((source) => <SourceRow key={source.id} source={source} policy={data.policy} diagnostic={diagnostics[source.id]} busy={busy} onRun={(capability) => void runBatch([source.id], capability)} onToggle={(capability, enabled) => setPending({ source: source.id, sourceName: source.display_name, capability, enabled })} />)}</tbody>
        </table>
      </div>
    </AdminSection>
    <AdminSection title="全局检索策略" icon={<Settings2 className="h-5 w-5 text-accent" />}>
      <div className="grid gap-4 text-sm md:grid-cols-3">
        <label>路由模式<select value={data.policy.routing_mode} disabled={busy} onChange={(event) => void updateGlobalPolicy({ routing_mode: event.target.value as PaperSearchPolicy["routing_mode"] })} className="mt-1 h-9 w-full rounded-lg border border-border-light bg-bg px-2"><option value="smart">智能路由</option><option value="all_enabled">全部启用平台</option></select></label>
        <label>检索总时限（秒）<input type="number" min={10} max={30} value={data.policy.search_deadline_seconds} disabled={busy} onChange={(event) => void updateGlobalPolicy({ search_deadline_seconds: Number(event.target.value) })} className="mt-1 h-9 w-full rounded-lg border border-border-light bg-bg px-2" /></label>
        <label>单平台时限（秒）<input type="number" min={3} max={30} value={data.policy.per_source_timeout_seconds} disabled={busy} onChange={(event) => void updateGlobalPolicy({ per_source_timeout_seconds: Number(event.target.value) })} className="mt-1 h-9 w-full rounded-lg border border-border-light bg-bg px-2" /></label>
      </div>
    </AdminSection>
    {pending && <ConfirmModal title="确认修改平台能力" body={`${pending.enabled ? "开启" : "关闭"}${pending.sourceName}的${pending.capability === "search" ? "论文搜索" : "摘要获取"}能力。`} confirmLabel="确认" onConfirm={() => void confirmToggle()} onClose={() => setPending(null)} busy={busy} />}
  </div>;
}

function SourceRow({ source, policy, diagnostic, busy, onRun, onToggle }: { source: PaperSearchPolicyResponse["source_catalog"][number]; policy: PaperSearchPolicy; diagnostic?: PlatformDiagnostic; busy: boolean; onRun: (capability: "connectivity" | PaperCapability) => void; onToggle: (capability: PaperCapability, enabled: boolean) => void }) {
  const states = policy.capabilities[source.id] || {};
  const latest = Math.max(0, ...Object.values(states).map((s) => s.last_checked_at || 0));
  return <tr className="border-t border-border-light align-top"><td className="px-3 py-3"><div className="font-medium text-fg">{source.display_name}</div><div className="mt-1 text-[11px] text-muted">{source.id}</div></td><td className="px-3 py-3"><span className={`rounded-full border px-2 py-0.5 text-[11px] ${tone(source.configuration_status === "ready" ? "ok" : "not_configured")}`}>{configurationLabel(source.configuration_status)}</span><div className="mt-1 text-[11px] text-muted">{diagnosticMethodLabel()}</div></td>{CAPABILITIES.map(({ key, label, action }) => { const state = states[key]; const metric = diagnostic?.[key]; const supported = supportsCapability(source, key); if (!supported) return <td key={key} className="px-3 py-3 text-muted">不适用</td>; return <td key={key} className="px-3 py-3"><div className="flex items-center gap-2"><AdminToggle checked={Boolean(state?.enabled)} disabled={busy} onChange={(enabled) => onToggle(key, enabled)} label="" /><span className={`rounded-full border px-2 py-0.5 text-[11px] ${tone(metric?.status || state?.last_diagnostic_status)}`}>{statusLabel(metric?.status || state?.last_diagnostic_status)}</span></div><div className="mt-1 text-[11px] text-muted">{metricText(key, metric, state)}</div><button disabled={busy} onClick={() => onRun(key)} className="mt-1 text-[11px] text-accent disabled:opacity-50">{action}</button></td>; })}<td className="px-3 py-3 text-[11px] text-muted">{latest ? new Date(latest * 1000).toLocaleString() : diagnostic ? "本页刚完成检测" : "尚未检测"}</td><td className="max-w-[260px] px-3 py-3 text-[11px] leading-relaxed text-muted">{source.coverage}<br />{source.operational_reason}</td></tr>;
}
