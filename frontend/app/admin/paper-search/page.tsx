"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Activity, CheckCircle2, ChevronDown, ChevronUp, Gauge, Globe2, Loader2,
  Play, RefreshCw, Search, Settings2, XCircle,
} from "lucide-react";
import {
  AdminHeader, AdminSection, AdminToggle, ConfirmModal,
} from "@/components/admin/AdminUI";
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
  { key: "fulltext", label: "全文获取", action: "测速" },
];

type Filter = "all" | "ok" | "slow" | "failed" | "disabled" | "not_configured" | "unchecked";

type PendingToggle = {
  source: string; sourceName: string; capability: PaperCapability; enabled: boolean;
};

function statusLabel(status?: string | null): string {
  return ({
    ok: "正常", slow: "缓慢", failed: "失败", not_applicable: "不适用",
    not_configured: "配置缺失", empty: "结果为空",
  } as Record<string, string>)[status || ""] || "未检测";
}

function tone(status?: string | null): string {
  if (status === "ok") return "border-emerald-500/30 bg-emerald-500/10 text-emerald-500";
  if (status === "slow" || status === "empty") return "border-amber-500/30 bg-amber-500/10 text-amber-500";
  if (status === "failed") return "border-red-500/30 bg-red-500/10 text-red-500";
  return "border-border-light bg-surface-hover text-muted";
}

function metricText(capability: PaperCapability, metric?: CapabilityDiagnostic | null, state?: PaperCapabilityState): string {
  if (metric) {
    if (capability === "search" && metric.result_count !== undefined) {
      return `${metric.result_count} 篇 · ${metric.latency_ms ?? "—"} ms`;
    }
    if (capability === "abstract" && metric.abstract_length !== undefined) {
      return `${metric.valid_abstract_count ?? 1} 条 · ${metric.abstract_length} 字 · ${metric.latency_ms ?? "—"} ms`;
    }
    if (capability === "fulltext" && metric.bytes_read !== undefined) {
      return `${metric.pdf_magic_valid ? "PDF 有效" : "PDF 未验证"} · ${metric.bytes_read} B · ${metric.kb_per_second ?? 0} KB/s`;
    }
    return metric.latency_ms != null ? `${metric.latency_ms} ms` : (metric.message || "—");
  }
  if (capability === "fulltext" && state?.last_kb_per_second != null) return `${state.last_kb_per_second} KB/s`;
  if (state?.last_latency_ms != null) return `${state.last_latency_ms} ms`;
  return "尚无检测指标";
}

function impactText(source: string, capability: PaperCapability, enabled: boolean): string {
  const labels = { search: "搜索标题、作者和 DOI", abstract: "读取或使用该平台摘要", fulltext: "OA 探测和远程 PDF 下载" };
  if (enabled) return `恢复「${source}」${CAPABILITIES.find((item) => item.key === capability)?.label}后，普通 Agent 将在下一次任务中重新允许${labels[capability]}。检测成功不会自动恢复，只有本次管理员确认会恢复。`;
  const retained = capability === "abstract" ? "Agent 仍可搜索标题和 DOI，但不会读取或使用该平台摘要。" : capability === "fulltext" ? "已验证的本地缓存仍可使用，但不会发起新的 OA 解析、探测或下载。" : "其他已启用平台仍会继续检索。";
  return `关闭「${source}」${CAPABILITIES.find((item) => item.key === capability)?.label}后，将停止${labels[capability]}。${retained}`;
}

function supportsCapability(
  source: PaperSearchPolicyResponse["source_catalog"][number],
  capability: PaperCapability,
): boolean {
  if (capability === "search") return source.supports_search;
  if (capability === "abstract") return source.supports_abstract;
  return source.supports_fulltext;
}


function diagnosticMethodLabel(method: string): string {
  if (method === "official_api_plus_controlled_pdf_probe") {
    return "官方 API + 受控 PDF 探测";
  }
  if (method === "official_resolver") return "官方解析器";
  return "官方 API";
}

function configurationLabel(status: string): string {
  return ({
    ready: "配置就绪",
    missing_api_key: "缺少 API Key",
    missing_contact_email: "缺少联系邮箱",
    license_not_confirmed: "许可未确认",
    incomplete_credentials: "凭证不完整",
    index_empty: "本地索引为空",
    index_unavailable: "本地索引不可用",
  } as Record<string, string>)[status] || status;
}


export default function PaperSearchAdminPage() {
  const router = useRouter();
  const user = useAuthStore((state) => state.user);
  const [data, setData] = useState<PaperSearchPolicyResponse | null>(null);
  const [diagnostics, setDiagnostics] = useState<Record<string, PlatformDiagnostic>>({});
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [activeSource, setActiveSource] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [pendingToggle, setPendingToggle] = useState<PendingToggle | null>(null);
  const [pendingRestore, setPendingRestore] = useState<{ source: string; sourceName: string; capabilities: PaperCapability[] } | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    setError("");
    try {
      const [policy, latest] = await Promise.all([getPaperSearchPolicy(), getLatestPaperDiagnostics()]);
      setData(policy);
      const platforms = latest.capability?.platforms || [];
      if (platforms.length) setDiagnostics(Object.fromEntries(platforms.map((item) => [item.source, item])));
    } catch (err) {
      if (err instanceof AdminApiError && err.status === 403) { router.replace("/chat"); return; }
      setError(err instanceof Error ? err.message : "加载失败");
    }
  }, [router]);

  useEffect(() => { if (user && user.role !== "administrator") router.replace("/chat"); }, [router, user]);
  useEffect(() => { void load(); }, [load]);

  const rows = useMemo(() => {
    if (!data) return [];
    return data.source_catalog.filter((source) => {
      if (query && !`${source.display_name} ${source.id} ${source.coverage}`.toLowerCase().includes(query.toLowerCase())) return false;
      const states = data.policy.capabilities[source.id];
      const diagnostic = diagnostics[source.id];
      const statuses = diagnostic ? [diagnostic.connectivity.status, diagnostic.search.status, diagnostic.abstract.status, diagnostic.fulltext.status] : [];
      if (filter === "ok") return statuses.includes("ok") && !statuses.includes("failed");
      if (filter === "slow") return statuses.includes("slow") || statuses.includes("empty");
      if (filter === "failed") return statuses.includes("failed");
      if (filter === "disabled") {
        return Boolean(states && CAPABILITIES.some(
          ({ key }) => supportsCapability(source, key) && !states[key]?.enabled,
        ));
      }
      if (filter === "not_configured") return source.configuration_status !== "ready" || statuses.includes("not_configured");
      if (filter === "unchecked") return !diagnostic && (!states || Object.values(states).every((state) => !state.last_checked_at));
      return true;
    });
  }, [data, diagnostics, filter, query]);

  if (!data) return <main className="min-h-screen bg-bg"><div className="mx-auto max-w-7xl p-8 text-sm text-muted">{error || "加载中…"}</div></main>;

  const policy = data.policy;
  const registered = new Set(Object.keys(policy.capabilities));
  const disabledPlatforms = data.source_catalog.filter((source) => {
    const states = policy.capabilities[source.id];
    return Boolean(states && CAPABILITIES.some(
      ({ key }) => supportsCapability(source, key) && !states[key]?.enabled,
    ));
  }).length;
  const healthy = data.source_catalog.filter((source) => {
    const d = diagnostics[source.id];
    return d && d.connectivity.status === "ok" && ![d.search, d.abstract, d.fulltext].some((item) => item.status === "failed");
  }).length;

  const runBatch = async (sources: string[], capability?: "connectivity" | PaperCapability) => {
    if (busy || !sources.length) return;
    setBusy(true); setError(""); setNotice("");
    try {
      let lastCompleteAt: number | null = null;
      for (const source of sources) {
        setActiveSource(source);
        const result = await runPaperCapabilityDiagnostics([source], capability);
        const platform = result.platforms?.[0];
        if (platform) setDiagnostics((old) => ({ ...old, [source]: platform }));
        if (!capability) lastCompleteAt = result.finished_at;
        if (result.policy) {
          setData((old) => old ? {
            ...old,
            policy: result.policy!,
            capabilities: result.policy!.capabilities,
            last_checked_at: lastCompleteAt ?? old.last_checked_at,
          } : old);
        }
      }
      const capabilityLabel = capability === "connectivity"
        ? "基础连通"
        : CAPABILITIES.find((item) => item.key === capability)?.label;
      setNotice(`已完成 ${sources.length} 个平台的${capabilityLabel || "完整"}检测；失败能力已按规则持久关闭，成功检测不会自动恢复。`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "检测失败");
    } finally { setActiveSource(null); setBusy(false); }
  };

  const applyToggle = async () => {
    if (!pendingToggle) return;
    setBusy(true); setError("");
    try {
      const result = await setPaperSourceCapability(pendingToggle.source, pendingToggle.capability, pendingToggle.enabled, policy.version);
      setData((old) => old ? { ...old, policy: result.policy, capabilities: result.policy.capabilities } : old);
      setNotice(`${pendingToggle.sourceName} ${CAPABILITIES.find((item) => item.key === pendingToggle.capability)?.label}已${pendingToggle.enabled ? "恢复" : "关闭"}。`);
      setPendingToggle(null);
    } catch (err) { setError(err instanceof Error ? err.message : "修改失败"); }
    finally { setBusy(false); }
  };

  const applyRestore = async () => {
    if (!pendingRestore) return;
    setBusy(true); setError("");
    try {
      let currentPolicy = policy;
      for (const capability of pendingRestore.capabilities) {
        const result = await setPaperSourceCapability(
          pendingRestore.source, capability, true, currentPolicy.version,
        );
        currentPolicy = result.policy;
      }
      setData((old) => old ? { ...old, policy: currentPolicy, capabilities: currentPolicy.capabilities } : old);
      setNotice(`${pendingRestore.sourceName} 被诊断自动关闭的能力已恢复；历史检测记录仍保留。`);
      setPendingRestore(null);
    } catch (err) { setError(err instanceof Error ? err.message : "恢复失败"); }
    finally { setBusy(false); }
  };

  const updateGlobalPolicy = async (
    changes: Parameters<typeof updatePaperSearchPolicy>[1],
  ) => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const result = await updatePaperSearchPolicy(policy.version, changes);
      setData((old) => old ? {
        ...old, policy: result.policy, capabilities: result.policy.capabilities,
      } : old);
    } catch (err) {
      setError(err instanceof Error ? err.message : "全局策略修改失败");
    } finally {
      setBusy(false);
    }
  };

  const toggleSelected = (source: string) => setSelected((old) => old.includes(source) ? old.filter((id) => id !== source) : [...old, source]);
  const allVisibleSelected = rows.length > 0 && rows.every((row) => selected.includes(row.id));

  return <main className="min-h-screen bg-bg pb-12">
    <AdminHeader current="/admin/paper-search" title="论文平台能力矩阵"
      subtitle="统一管理搜索、摘要与全文能力；管理员检测可绕过关闭状态，但普通 Agent 不可绕过。"
      onRefresh={async () => { setRefreshing(true); await load(); setRefreshing(false); }} refreshing={refreshing} />

    <div className="mx-auto max-w-[1600px] space-y-4 px-4 py-6 sm:px-8">
      {error && <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-500">{error}</div>}
      {notice && <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-500">{notice}</div>}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Summary label="平台总数" value={data.source_catalog.length} icon={<Globe2 className="h-4 w-4" />} />
        <Summary label="当前正常" value={healthy} icon={<CheckCircle2 className="h-4 w-4" />} />
        <Summary label="存在关闭能力" value={disabledPlatforms} icon={<XCircle className="h-4 w-4" />} />
        <Summary label="最近完整检测" value={data.last_checked_at ? new Date(data.last_checked_at * 1000).toLocaleString() : "尚未检测"} icon={<Activity className="h-4 w-4" />} compact />
      </div>

      <AdminSection title="平台能力矩阵" icon={<Gauge className="h-5 w-5 text-accent" />}>
        <div className="mb-4 flex flex-wrap items-center gap-2">
          <label className="relative min-w-[220px] flex-1 lg:max-w-sm">
            <Search className="absolute left-3 top-2.5 h-4 w-4 text-muted" />
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索平台或覆盖领域"
              className="h-9 w-full rounded-lg border border-border-light bg-bg pl-9 pr-3 text-sm outline-none focus:border-accent" />
          </label>
          <select value={filter} onChange={(event) => setFilter(event.target.value as Filter)}
            className="h-9 rounded-lg border border-border-light bg-bg px-3 text-sm">
            <option value="all">全部状态</option><option value="ok">正常</option><option value="slow">缓慢/空结果</option>
            <option value="failed">失败</option><option value="disabled">存在关闭能力</option>
            <option value="not_configured">配置缺失</option><option value="unchecked">未检测</option>
          </select>
          <button disabled={busy || !selected.length} onClick={() => void runBatch(selected)} className="flex h-9 items-center gap-1.5 rounded-lg border border-border-light px-3 text-sm disabled:opacity-50">
            {busy && selected.includes(activeSource || "") ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}检测选中（{selected.length}）
          </button>
          <button disabled={busy} onClick={() => void runBatch(data.source_catalog.map((item) => item.id))} className="flex h-9 items-center gap-1.5 rounded-lg bg-accent px-4 text-sm text-white disabled:opacity-50">
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}完整检测全部
          </button>
        </div>

        <div className="hidden overflow-x-auto rounded-xl border border-border-light md:block">
          <table className="w-full min-w-[1280px] text-left text-sm">
            <thead className="sticky top-0 z-10 bg-surface text-xs text-muted"><tr className="border-b border-border-light">
              <th className="p-3"><input type="checkbox" checked={allVisibleSelected} onChange={() => setSelected(allVisibleSelected ? selected.filter((id) => !rows.some((row) => row.id === id)) : Array.from(new Set([...selected, ...rows.map((row) => row.id)])))} /></th>
              <th className="p-3">平台</th><th className="p-3">基础连通</th>
              {CAPABILITIES.map((item) => <th key={item.key} className="p-3">{item.label}</th>)}
              <th className="p-3">最近检测 / 原因</th><th className="p-3">操作</th>
            </tr></thead>
            <tbody>{rows.map((source) => <PlatformRow key={source.id} source={source} policy={policy}
              diagnostic={diagnostics[source.id]} selected={selected.includes(source.id)} active={activeSource === source.id}
              registered={registered.has(source.id)} expanded={expanded === source.id}
              onSelect={() => toggleSelected(source.id)} onExpand={() => setExpanded(expanded === source.id ? null : source.id)}
              onToggle={(capability, enabled) => setPendingToggle({ source: source.id, sourceName: source.display_name, capability, enabled })}
              onRestore={(capabilities) => setPendingRestore({ source: source.id, sourceName: source.display_name, capabilities })}
              onRun={(capability) => void runBatch([source.id], capability)} busy={busy} />)}</tbody>
          </table>
        </div>

        <div className="space-y-3 md:hidden">{rows.map((source) => <PlatformCard key={source.id} source={source}
          policy={policy} diagnostic={diagnostics[source.id]} selected={selected.includes(source.id)} active={activeSource === source.id}
          registered={registered.has(source.id)} onSelect={() => toggleSelected(source.id)}
          onToggle={(capability, enabled) => setPendingToggle({ source: source.id, sourceName: source.display_name, capability, enabled })}
          onRestore={(capabilities) => setPendingRestore({ source: source.id, sourceName: source.display_name, capabilities })}
          onRun={(capability) => void runBatch([source.id], capability)} busy={busy} />)}</div>
      </AdminSection>

      <AdminSection title="全局检索与全文策略" icon={<Settings2 className="h-5 w-5 text-accent" />}>
        <div className="grid gap-4 text-sm md:grid-cols-2 xl:grid-cols-4">
          <label>
            路由模式
            <select
              value={policy.routing_mode}
              disabled={busy}
              onChange={(event) => void updateGlobalPolicy({
                routing_mode: event.target.value as PaperSearchPolicy["routing_mode"],
              })}
              className="mt-1 h-9 w-full rounded-lg border border-border-light bg-bg px-2 disabled:opacity-50"
            >
              <option value="smart">智能路由</option>
              <option value="all_enabled">全部启用平台</option>
            </select>
          </label>
          <label>
            检索总时限（秒）
            <input
              type="number" min={10} max={30}
              value={policy.search_deadline_seconds}
              disabled={busy}
              onChange={(event) => {
                const value = Number(event.target.value);
                if (value < policy.per_source_timeout_seconds) return;
                void updateGlobalPolicy({ search_deadline_seconds: value });
              }}
              className="mt-1 h-9 w-full rounded-lg border border-border-light bg-bg px-2 disabled:opacity-50"
            />
          </label>
          <label>
            单平台时限（秒）
            <input
              type="number" min={3} max={30}
              value={policy.per_source_timeout_seconds}
              disabled={busy}
              onChange={(event) => {
                const value = Number(event.target.value);
                if (value > policy.search_deadline_seconds) return;
                void updateGlobalPolicy({ per_source_timeout_seconds: value });
              }}
              className="mt-1 h-9 w-full rounded-lg border border-border-light bg-bg px-2 disabled:opacity-50"
            />
          </label>
          <label>
            远程全文总策略
            <select
              value={policy.paper_fetch_mode}
              disabled={busy}
              onChange={(event) => void updateGlobalPolicy({
                paper_fetch_mode: event.target.value as PaperSearchPolicy["paper_fetch_mode"],
              })}
              className="mt-1 h-9 w-full rounded-lg border border-border-light bg-bg px-2 disabled:opacity-50"
            >
              <option value="enabled">完全开启</option>
              <option value="explicit_only">仅明确深读</option>
              <option value="probe_only">仅探测</option>
              <option value="disabled">完全关闭</option>
            </select>
          </label>
          <label>
            全文探测时限（秒）
            <input
              type="number" min={0} max={30}
              value={policy.fulltext_verify_timeout_seconds}
              disabled={busy}
              onChange={(event) => void updateGlobalPolicy({
                fulltext_verify_timeout_seconds: Number(event.target.value),
              })}
              className="mt-1 h-9 w-full rounded-lg border border-border-light bg-bg px-2 disabled:opacity-50"
            />
          </label>
          <label>
            策略提示方式
            <select
              value={policy.fetch_policy_disclosure}
              disabled={busy}
              onChange={(event) => void updateGlobalPolicy({
                fetch_policy_disclosure: event.target.value as PaperSearchPolicy["fetch_policy_disclosure"],
              })}
              className="mt-1 h-9 w-full rounded-lg border border-border-light bg-bg px-2 disabled:opacity-50"
            >
              <option value="affected_only">受影响时提示</option>
              <option value="silent">静默执行</option>
            </select>
          </label>
          <div className="rounded-lg border border-border-light p-3">
            <AdminToggle
              checked={policy.verify_fulltext}
              disabled={busy}
              onChange={(enabled) => void updateGlobalPolicy({ verify_fulltext: enabled })}
              label="检索后验证 OA 全文"
            />
            <p className="mt-1 text-xs text-muted">关闭后搜索结果保持待验证，深读时仍按能力 gate 实际下载。</p>
          </div>
          <div className="rounded-lg border border-border-light p-3">
            <AdminToggle
              checked={policy.force_fulltext_probe}
              disabled={busy}
              onChange={(enabled) => void updateGlobalPolicy({ force_fulltext_probe: enabled })}
              label="时间紧张时仍预留探测预算"
            />
            <p className="mt-1 text-xs text-muted">开启后会压缩检索/重排时间，为轻量 PDF 探测保留尾部预算。</p>
          </div>
        </div>
      </AdminSection>
    </div>

    {pendingToggle && <ConfirmModal title={pendingToggle.enabled ? "恢复平台能力" : "关闭平台能力"}
      body={impactText(pendingToggle.sourceName, pendingToggle.capability, pendingToggle.enabled)}
      danger={!pendingToggle.enabled} busy={busy} confirmLabel={pendingToggle.enabled ? "确认恢复" : "确认关闭"}
      onConfirm={() => void applyToggle()} onClose={() => setPendingToggle(null)} />}
    {pendingRestore && <ConfirmModal title="恢复平台自动关闭能力"
      body={`确认恢复「${pendingRestore.sourceName}」被诊断自动关闭的 ${pendingRestore.capabilities.length} 项能力？检测成功不会自动恢复；本次确认后普通 Agent 会在下一次任务重新允许这些网络访问。`}
      busy={busy} confirmLabel="确认全部恢复" onConfirm={() => void applyRestore()} onClose={() => setPendingRestore(null)} />}
  </main>;
}

function Summary({ label, value, icon, compact = false }: { label: string; value: string | number; icon: React.ReactNode; compact?: boolean }) {
  return <div className="rounded-xl border border-border-light bg-surface p-4"><div className="flex items-center gap-2 text-xs text-muted">{icon}{label}</div><div className={`mt-2 font-semibold ${compact ? "text-sm" : "text-2xl"}`}>{value}</div></div>;
}

type RowProps = {
  source: PaperSearchPolicyResponse["source_catalog"][number]; policy: PaperSearchPolicy;
  diagnostic?: PlatformDiagnostic; selected: boolean; active: boolean; registered: boolean; busy: boolean;
  onSelect: () => void; onToggle: (capability: PaperCapability, enabled: boolean) => void;
  onRestore: (capabilities: PaperCapability[]) => void;
  onRun: (capability?: "connectivity" | PaperCapability) => void;
};

function PlatformRow(props: RowProps & { expanded: boolean; onExpand: () => void }) {
  const { source, policy, diagnostic, registered } = props;
  return <>
    <tr className="border-b border-border-light/70 align-top">
      <td className="p-3"><input type="checkbox" checked={props.selected} onChange={props.onSelect} /></td>
      <td className="p-3">
        <div className="font-medium">{source.display_name}</div>
        <div className="mt-1 text-xs text-muted">
          {source.protocol} · {source.coverage}
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-1 text-[11px] text-muted">
          <a
            href={source.official_docs_url}
            target="_blank"
            rel="noreferrer"
            className="text-accent hover:underline"
          >
            官方文档
          </a>
          <span>·</span>
          <span>
            {diagnosticMethodLabel(source.diagnostic_method)}
          </span>
        </div>
        <div className="mt-1 flex flex-wrap gap-1 text-[11px]">
          <span className={props.source.configuration_status === "ready" ? "text-emerald-500" : "text-amber-500"}>
            {configurationLabel(props.source.configuration_status)}
          </span>
          {props.source.license_confirmation_status === "not_confirmed" && (
            <span className="text-amber-500">· 许可未确认</span>
          )}
          {props.source.local_index_status && (
            <span className="text-muted">
              · 本地索引 {Number(props.source.local_index_status.indexed_count || 0)} 条
            </span>
          )}
        </div>
        {props.active && (
          <div className="mt-1 flex items-center gap-1 text-xs text-accent">
            <Loader2 className="h-3 w-3 animate-spin" />
            管理员强制检测中
          </div>
        )}
      </td>
      <td className="p-3"><DiagnosticBadge metric={diagnostic?.connectivity} /><button disabled={props.busy} onClick={() => props.onRun("connectivity")} className="mt-2 text-xs text-accent disabled:opacity-50">检测连通</button></td>
      {CAPABILITIES.map(({ key, action }) => <td key={key} className="p-3"><CapabilityCell capability={key} action={action} source={source} state={policy.capabilities[source.id]?.[key]} metric={diagnostic?.[key]} registered={registered} busy={props.busy} onToggle={props.onToggle} onRun={props.onRun} /></td>)}
      <td className="max-w-[220px] p-3 text-xs"><div>{latestTime(policy.capabilities[source.id], diagnostic)}</div><div className="mt-1 line-clamp-3 text-muted" title={reasonText(policy.capabilities[source.id], diagnostic)}>{reasonText(policy.capabilities[source.id], diagnostic) || "无关闭原因"}</div></td>
      <td className="p-3"><button disabled={props.busy} onClick={() => props.onRun()} className="rounded-lg bg-accent px-3 py-1.5 text-xs text-white disabled:opacity-50">完整检测</button>{diagnosticDisabled(policy.capabilities[source.id]).length > 0 && <button disabled={props.busy} onClick={() => props.onRestore(diagnosticDisabled(policy.capabilities[source.id]))} className="mt-2 block text-xs text-emerald-500 disabled:opacity-50">恢复自动关闭</button>}<button onClick={props.onExpand} className="mt-2 flex items-center gap-1 text-xs text-muted">详情{props.expanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}</button></td>
    </tr>
    {props.expanded && <tr className="border-b border-border-light bg-bg/60"><td colSpan={8} className="p-4 text-xs text-muted"><pre className="whitespace-pre-wrap break-all">{JSON.stringify(diagnostic || { message: "尚无诊断详情" }, null, 2)}</pre></td></tr>}
  </>;
}

function PlatformCard(props: RowProps) {
  const disabled = diagnosticDisabled(props.policy.capabilities[props.source.id]);
  return (
    <article className="rounded-xl border border-border-light bg-bg p-4">
      <div className="flex items-start justify-between gap-3">
        <label className="flex items-start gap-2">
          <input
            type="checkbox"
            checked={props.selected}
            onChange={props.onSelect}
            className="mt-1"
          />
          <span>
            <strong>{props.source.display_name}</strong>
            <span className="mt-1 block text-xs text-muted">
              {props.source.coverage}
            </span>
            <span className="mt-1 flex flex-wrap items-center gap-1 text-[11px] text-muted">
              <a
                href={props.source.official_docs_url}
                target="_blank"
                rel="noreferrer"
                className="text-accent hover:underline"
              >
                官方文档
              </a>
              <span>·</span>
              <span>
                {diagnosticMethodLabel(props.source.diagnostic_method)}
              </span>
            </span>
            <span className={`mt-1 block text-[11px] ${
              props.source.configuration_status === "ready"
                ? "text-emerald-500" : "text-amber-500"
            }`}>
              {configurationLabel(props.source.configuration_status)}
              {props.source.local_index_status
                ? ` · 本地索引 ${Number(props.source.local_index_status.indexed_count || 0)} 条`
                : ""}
            </span>
            {props.active && (
              <span className="mt-1 flex items-center gap-1 text-xs text-accent">
                <Loader2 className="h-3 w-3 animate-spin" />
                管理员强制检测中
              </span>
            )}
          </span>
        </label>
        <div className="flex flex-col items-end gap-2">
          <button
            disabled={props.busy}
            onClick={() => props.onRun()}
            className="rounded-lg bg-accent px-3 py-1.5 text-xs text-white disabled:opacity-50"
          >
            完整检测
          </button>
          {disabled.length > 0 && (
            <button
              disabled={props.busy}
              onClick={() => props.onRestore(disabled)}
              className="text-xs text-emerald-500 disabled:opacity-50"
            >
              恢复自动关闭
            </button>
          )}
        </div>
      </div>
      <div className="mt-3 rounded-lg border border-border-light p-3 text-xs">
        <div className="flex items-center justify-between gap-2">
          <span>基础连通</span>
          <DiagnosticBadge metric={props.diagnostic?.connectivity} />
        </div>
        <button
          disabled={props.busy}
          onClick={() => props.onRun("connectivity")}
          className="mt-2 text-xs text-accent disabled:opacity-50"
        >
          检测连通
        </button>
      </div>
      <div className="mt-3 grid gap-2">
        {CAPABILITIES.map(({ key, label, action }) => (
          <div key={key} className="rounded-lg border border-border-light p-3">
            <div className="mb-2 font-medium">{label}</div>
            <CapabilityCell
              capability={key}
              action={action}
              source={props.source}
              state={props.policy.capabilities[props.source.id]?.[key]}
              metric={props.diagnostic?.[key]}
              registered={props.registered}
              busy={props.busy}
              onToggle={props.onToggle}
              onRun={props.onRun}
            />
          </div>
        ))}
      </div>
      <details className="mt-3 rounded-lg border border-border-light p-3 text-xs text-muted">
        <summary className="cursor-pointer">完整诊断详情</summary>
        <pre className="mt-2 whitespace-pre-wrap break-all">
          {JSON.stringify(props.diagnostic || { message: "尚无诊断详情" }, null, 2)}
        </pre>
      </details>
    </article>
  );
}

function CapabilityCell({ capability, action, source, state, metric, registered, busy, onToggle, onRun }: {
  capability: PaperCapability; action: string; source: RowProps["source"]; state?: PaperCapabilityState;
  metric?: CapabilityDiagnostic; registered: boolean; busy: boolean;
  onToggle: RowProps["onToggle"]; onRun: RowProps["onRun"];
}) {
  const supported = supportsCapability(source, capability);
  if (!supported) {
    return (
      <div>
        <DiagnosticBadge metric={{
          status: "not_applicable", latency_ms: null, error_code: null,
          message: "该平台不提供此能力",
        }} />
        <div className="mt-2 text-xs text-muted">不适用</div>
      </div>
    );
  }
  if (!registered) {
    return (
      <div>
        <DiagnosticBadge metric={metric} />
        <div className="mt-2 text-xs text-muted">
          {metricText(capability, metric)}
        </div>
        <div className="mt-1 text-xs text-muted">辅助检测目标，无独立开关</div>
        <button
          disabled={busy}
          onClick={() => onRun(capability)}
          className="mt-2 text-xs text-accent disabled:opacity-50"
        >
          {action}
        </button>
      </div>
    );
  }
  const displayStatus = !state?.enabled ? "failed" : metric?.status || state?.last_diagnostic_status;
  return <div><div className="flex items-center gap-2"><AdminToggle checked={Boolean(state?.enabled)} disabled={busy} onChange={(enabled) => onToggle(capability, enabled)} label={`${source.display_name}${capability}`} /><DiagnosticBadge metric={metric} status={displayStatus} /></div><div className="mt-2 text-xs text-muted">{metricText(capability, metric, state)}</div>{state?.disabled_by && <div className="mt-1 text-xs text-red-500">{state.disabled_by === "diagnostic" ? "已自动关闭" : "管理员已关闭"}</div>}<button disabled={busy} onClick={() => onRun(capability)} className="mt-2 text-xs text-accent disabled:opacity-50">{action}</button></div>;
}

function DiagnosticBadge({ metric, status }: { metric?: CapabilityDiagnostic; status?: string | null }) {
  const value = status || metric?.status || null;
  return <span title={metric?.message || metric?.error_code || undefined} className={`inline-flex rounded-full border px-2 py-0.5 text-[11px] ${tone(value)}`}>{statusLabel(value)}</span>;
}

function diagnosticDisabled(states?: Record<PaperCapability, PaperCapabilityState>): PaperCapability[] {
  if (!states) return [];
  return CAPABILITIES.map((item) => item.key).filter((capability) => states[capability]?.disabled_by === "diagnostic");
}

function latestTime(states?: Record<PaperCapability, PaperCapabilityState>, diagnostic?: PlatformDiagnostic): string {
  const times = states ? Object.values(states).map((state) => state.last_checked_at || 0) : [];
  const latest = Math.max(0, ...times);
  return latest ? new Date(latest * 1000).toLocaleString() : diagnostic ? "本页刚完成检测" : "尚未检测";
}

function reasonText(states?: Record<PaperCapability, PaperCapabilityState>, diagnostic?: PlatformDiagnostic): string {
  const reasons = states ? Object.values(states).map((state) => state.reason).filter(Boolean) : [];
  if (reasons.length) return reasons.join("；");
  return [diagnostic?.connectivity, diagnostic?.search, diagnostic?.abstract, diagnostic?.fulltext].map((item) => item?.message || item?.error_code).filter(Boolean).join("；");
}
