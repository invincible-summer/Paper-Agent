"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Activity, Check, Download, Gauge, Globe2, Loader2, RotateCcw, Save, Search,
} from "lucide-react";
import {
  AdminHeader, AdminSection, AdminToggle, HelpModal, InfoButton, type HelpEntry,
} from "@/components/admin/AdminUI";
import {
  AdminApiError, getLatestPaperDiagnostics, getPaperSearchPolicy,
  runPaperConnectivity, runPaperDownloadTest, updatePaperSearchPolicy,
  type FetchPolicyDisclosure, type PaperDiagnosticRun, type PaperFetchMode,
  type PaperSearchPolicy, type PaperSearchPolicyResponse,
} from "@/lib/admin-api";
import { useAuthStore } from "@/stores/auth";

const FETCH_MODES: Array<{ key: PaperFetchMode; name: string; desc: string }> = [
  { key: "enabled", name: "完全开启", desc: "允许全文探测、自动补读和显式深读下载，能力最完整。" },
  { key: "explicit_only", name: "仅明确深读时拉取", desc: "搜索可探测全文；只在直接 deep_read 时下载，自动问答不补下载。" },
  { key: "probe_only", name: "仅探测，不下载", desc: "可以判断 PDF 是否可用，但任何普通用户路径都不下载完整 PDF。" },
  { key: "disabled", name: "完全关闭远程全文", desc: "不解析远程全文地址、不探测、不下载，仅使用本地缓存和摘要。" },
];

const HELP: Record<string, HelpEntry> = {
  sources: { title: "论文渠道开关", entries: [
    ["关闭后的行为", "该渠道不参与普通用户检索，也不使用它提供的直接 PDF 地址；历史结果和本地缓存不会删除。"],
    ["管理员检测", "即使渠道已关闭，管理员仍可在下方显式点测，以判断恢复后是否重新开启。"],
    ["辅助全文服务", "Unpaywall 和 doi.org 不是元数据检索渠道，在下载测速区单独展示。"],
  ] },
  enabled: { title: "完全开启", entries: [
    ["行为", "允许 OA 候选解析、文件头探测、自动全文升级和显式 deep_read 下载。"],
    ["优点", "全文覆盖和自动研究能力最高。"],
    ["缺点", "跨境下载可能显著增加等待、网络和磁盘占用。"],
  ] },
  explicit_only: { title: "仅明确深读时拉取", entries: [
    ["行为", "保留全文探测；直接 deep_read 可以下载，ask_papers 等内部自动补读不会下载。"],
    ["优点", "普通问答更快，用户仍可按需阅读全文。"],
    ["缺点", "自动工作流可能只能基于摘要，用户需要明确深读。"],
  ] },
  probe_only: { title: "仅探测，不下载", entries: [
    ["行为", "允许解析 OA 地址和读取 PDF 文件头，但禁止完整文件下载。"],
    ["优点", "仍能显示全文可获取状态，不承担大文件下载成本。"],
    ["缺点", "未缓存论文无法做章节、图表和公式级深读。"],
  ] },
  disabled: { title: "完全关闭远程全文", entries: [
    ["行为", "禁止 Unpaywall/doi.org 全文解析、PDF 探测和下载；本地缓存仍可读取。"],
    ["优点", "速度、隐私和网络成本最可控。"],
    ["缺点", "新论文只能依赖摘要，全文状态显示为待验证。"],
  ] },
  disclosure: { title: "用户提示策略", entries: [
    ["受影响时提示", "只有本次深读或下载确实因策略降级时，才说明管理员限制。"],
    ["静默降级", "不在正文、工具卡或系统进度中暴露管理员策略；系统仍不会把摘要冒充全文。"],
    ["原始思考", "静默通过不注入策略原因实现，不对模型供应商原生思考内容做事后过滤。"],
  ] },
  performance: { title: "时间预算", entries: [
    ["单渠道时限", "一条渠道查询超过该时间后取消，不拖住其他渠道。"],
    ["检索总时限", "达到总预算后返回已经取得的部分结果。"],
    ["全文探测", "只读取 PDF 文件头，不下载全文；关闭后状态保持待验证。"],
    ["推荐", "云服务器建议单源 12 秒、检索 30 秒、全文探测 30 秒。"],
  ] },
};

function statusText(value: unknown): string {
  const labels: Record<string, string> = {
    ok: "正常", reachable_empty: "可达但无结果", rate_limited: "限流",
    timeout: "超时", connection_error: "连接失败", server_error: "服务端错误",
    http_error: "HTTP 异常", not_configured: "未配置", no_candidate: "无可测速 PDF",
    failed: "失败", not_pdf: "非 PDF",
  };
  return labels[String(value)] || String(value ?? "—");
}

export default function PaperSearchAdminPage() {
  const router = useRouter();
  const checked = useAuthStore((s) => s.checked);
  const user = useAuthStore((s) => s.user);
  const signOut = useAuthStore((s) => s.signOut);
  const [data, setData] = useState<PaperSearchPolicyResponse | null>(null);
  const [policy, setPolicy] = useState<PaperSearchPolicy | null>(null);
  const [draft, setDraft] = useState<PaperSearchPolicy | null>(null);
  const [latest, setLatest] = useState<{ connectivity: PaperDiagnosticRun | null; download: PaperDiagnosticRun | null } | null>(null);
  const [help, setHelp] = useState<HelpEntry | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState<"connectivity" | "download" | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [connectSources, setConnectSources] = useState<string[]>([]);
  const [downloadTargets, setDownloadTargets] = useState<string[]>(["arxiv", "unpaywall", "doi"]);

  useEffect(() => { void useAuthStore.getState().hydrate(); }, []);
  const handleError = useCallback((err: unknown) => {
    if (err instanceof AdminApiError && err.status === 401) {
      signOut(); router.replace("/login"); return;
    }
    setError(err instanceof Error ? err.message : "请求失败");
  }, [router, signOut]);

  const refresh = useCallback(async () => {
    setLoading(true); setError(""); setNotice("");
    try {
      const [policyData, diagnosticData] = await Promise.all([
        getPaperSearchPolicy(), getLatestPaperDiagnostics(),
      ]);
      setData(policyData); setPolicy(policyData.policy); setDraft(policyData.policy);
      setLatest({ connectivity: diagnosticData.connectivity, download: diagnosticData.download });
      setConnectSources(policyData.source_catalog.filter((item) => item.supports_connectivity).map((item) => item.id));
    } catch (err) { handleError(err); } finally { setLoading(false); }
  }, [handleError]);

  useEffect(() => {
    if (!checked) return;
    if (!user) { router.replace("/login"); return; }
    if (user.role !== "administrator") { setLoading(false); return; }
    void refresh();
  }, [checked, user, router, refresh]);

  const dirty = useMemo(() => Boolean(policy && draft && JSON.stringify(policy) !== JSON.stringify(draft)), [policy, draft]);
  const runtime = useMemo(() => new Map((data?.runtime_status || []).map((item) => [item.source, item])), [data]);
  const searchSources = data?.source_catalog.filter((item) => item.supports_search) || [];
  const connectivityCatalog = data?.source_catalog.filter((item) => item.supports_connectivity) || [];
  const downloadCatalog = data?.source_catalog.filter((item) => item.supports_download_test) || [];

  const save = async () => {
    if (!policy || !draft) return;
    setSaving(true); setError(""); setNotice("");
    try {
      const result = await updatePaperSearchPolicy(policy.version, {
        sources: draft.sources,
        search_deadline_seconds: draft.search_deadline_seconds,
        per_source_timeout_seconds: draft.per_source_timeout_seconds,
        verify_fulltext: draft.verify_fulltext,
        fulltext_verify_timeout_seconds: draft.fulltext_verify_timeout_seconds,
        paper_fetch_mode: draft.paper_fetch_mode,
        fetch_policy_disclosure: draft.fetch_policy_disclosure,
      });
      setPolicy(result.policy); setDraft(result.policy); setNotice("论文检索策略已保存，下一次请求立即生效。");
    } catch (err) { handleError(err); } finally { setSaving(false); }
  };

  const runConnectivity = async () => {
    setTesting("connectivity"); setError(""); setNotice("");
    try {
      const result = await runPaperConnectivity(connectSources);
      setLatest((old) => ({ connectivity: result, download: old?.download || null }));
      setNotice("连通性与真实检索检测完成。");
      const refreshed = await getPaperSearchPolicy(); setData(refreshed);
    } catch (err) { handleError(err); } finally { setTesting(null); }
  };

  const runDownload = async () => {
    setTesting("download"); setError(""); setNotice("");
    try {
      const result = await runPaperDownloadTest(downloadTargets);
      setLatest((old) => ({ connectivity: old?.connectivity || null, download: result }));
      setNotice("PDF 限量下载测速完成。");
    } catch (err) { handleError(err); } finally { setTesting(null); }
  };

  const toggleChoice = (values: string[], value: string, setter: (next: string[]) => void) => {
    setter(values.includes(value) ? values.filter((item) => item !== value) : [...values, value]);
  };

  if (!checked || loading) return <div className="flex min-h-screen items-center justify-center"><Loader2 className="h-6 w-6 animate-spin text-accent" /></div>;
  if (!user || user.role !== "administrator") return <div className="p-8 text-center text-muted">仅管理员可访问。</div>;
  if (!data || !policy || !draft) return <div className="p-8 text-center text-error">{error || "设置加载失败"}</div>;

  const enabledCount = Object.values(draft.sources).filter(Boolean).length;
  const openCount = data.runtime_status.filter((item) => item.state === "open").length;

  return <main className="min-h-screen bg-bg px-4 py-8 text-fg sm:px-8">
    <div className="mx-auto max-w-6xl space-y-6 pb-24">
      <AdminHeader title="论文检索" icon={<Search className="h-5 w-5" />}
        subtitle="管理论文渠道、远程全文拉取、超时与平台连通性。"
        current="/admin/paper-search" onRefresh={() => void refresh()} refreshing={loading} />

      {error && <div className="rounded-lg border border-error/40 bg-error/10 p-3 text-sm text-error">{error}</div>}
      {notice && <div className="rounded-lg border border-success/40 bg-success/10 p-3 text-sm text-success">{notice}</div>}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {[
          ["启用渠道", `${enabledCount} / 9`], ["熔断渠道", String(openCount)],
          ["检索总时限", `${draft.search_deadline_seconds}s`],
          ["全文策略", FETCH_MODES.find((item) => item.key === draft.paper_fetch_mode)?.name || draft.paper_fetch_mode],
        ].map(([label, value]) => <div key={label} className="rounded-xl border border-border-light bg-surface p-4">
          <div className="text-xs text-muted">{label}</div><div className="mt-1 text-xl font-semibold">{value}</div>
        </div>)}
      </div>

      <AdminSection title="论文检索渠道" icon={<Globe2 className="h-5 w-5 text-accent" />}
        info={<InfoButton onClick={() => setHelp(HELP.sources)} label="论文渠道开关说明" />}>
        <div className="mb-4 flex flex-wrap gap-2">
          <button type="button" onClick={() => setDraft({ ...draft, ...data.quick_preset, sources: data.quick_preset.sources || draft.sources })}
            className="rounded-lg border border-accent/40 bg-accent/10 px-3 py-2 text-sm text-accent hover:bg-accent/15">应用云服务器快速预设</button>
          <button type="button" onClick={() => setDraft({ ...draft, ...data.defaults, version: draft.version, updated_by: draft.updated_by, updated_at: draft.updated_at })}
            className="rounded-lg border border-border-light px-3 py-2 text-sm text-fg-secondary hover:bg-surface-hover">恢复初始默认值</button>
        </div>
        <div className="grid gap-3 lg:grid-cols-2">
          {searchSources.map((source) => {
            const state = runtime.get(source.id);
            const enabled = Boolean(draft.sources[source.id]);
            return <div key={source.id} className="flex items-center justify-between gap-4 rounded-xl border border-border-light p-4">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{source.display_name}</span>
                  {source.requires_key && <span className={`rounded px-1.5 py-0.5 text-[10px] ${source.key_configured ? "bg-success/15 text-success" : "bg-warning/15 text-warning"}`}>{source.key_configured ? "Key 已配置" : "Key 未配置"}</span>}
                  {state?.state === "open" && <span className="rounded bg-error/15 px-1.5 py-0.5 text-[10px] text-error">熔断 {Math.ceil(state.remaining_seconds)}s</span>}
                </div>
                <p className="mt-1 text-xs text-muted">{source.coverage}</p>
                <p className="mt-1 text-xs text-muted">{source.configuration_status}</p>
                <p className="mt-1 text-xs text-fg-secondary">HTTP {state?.last_http_status ?? "—"} · {state?.last_latency_ms ?? "—"} ms · {state?.last_error_code ? statusText(state.last_error_code) : "无异常"}</p>
              </div>
              <AdminToggle checked={enabled} label={`${source.display_name} 检索`}
                onChange={(next) => setDraft({ ...draft, sources: { ...draft.sources, [source.id]: next } })} />
            </div>;
          })}
        </div>
      </AdminSection>

      <AdminSection title="论文全文拉取策略" icon={<Download className="h-5 w-5 text-accent" />}>
        <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-4" role="radiogroup" aria-label="全文拉取策略">
          {FETCH_MODES.map((mode) => {
            const active = draft.paper_fetch_mode === mode.key;
            return <div key={mode.key} className={`relative rounded-xl border p-4 transition-colors ${active ? "border-accent bg-accent/10" : "border-border-light"}`}>
              <button type="button" role="radio" aria-checked={active} title={mode.desc}
                onClick={() => setDraft({ ...draft, paper_fetch_mode: mode.key })} className="w-full text-left">
                <div className="flex items-center justify-between gap-2"><span className={active ? "font-semibold text-accent" : "font-medium"}>{mode.name}</span>{active && <Check className="h-4 w-4 text-accent" />}</div>
                <p className="mt-2 text-xs leading-relaxed text-muted">{mode.desc}</p>
              </button>
              <div className="absolute right-2 top-2"><InfoButton onClick={() => setHelp(HELP[mode.key])} label={`${mode.name}优缺点`} /></div>
            </div>;
          })}
        </div>
        <div className="mt-4 border-t border-border-light pt-4">
          <div className="mb-2 flex items-center gap-1 text-sm font-medium">用户提示策略 <InfoButton onClick={() => setHelp(HELP.disclosure)} /></div>
          <div className="grid gap-2 sm:grid-cols-2">
            {([[
              "affected_only", "受影响时提示", "仅当本次深读或下载确实受限时向用户说明。",
            ], ["silent", "静默降级", "不暴露管理员策略，但仍不会把摘要冒充全文。"]] as Array<[FetchPolicyDisclosure, string, string]>).map(([key, name, desc]) =>
              <button key={key} type="button" onClick={() => setDraft({ ...draft, fetch_policy_disclosure: key })}
                className={`rounded-lg border p-3 text-left ${draft.fetch_policy_disclosure === key ? "border-accent bg-accent/10" : "border-border-light"}`}>
                <div className="text-sm font-medium">{name}</div><div className="mt-1 text-xs text-muted">{desc}</div>
              </button>)}
          </div>
        </div>
      </AdminSection>

      <AdminSection title="检索性能预算" icon={<Gauge className="h-5 w-5 text-accent" />}
        info={<InfoButton onClick={() => setHelp(HELP.performance)} label="时间预算说明" />}>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <label className="text-sm">单渠道时限（秒）<input type="number" min={3} max={30} value={draft.per_source_timeout_seconds}
            onChange={(e) => setDraft({ ...draft, per_source_timeout_seconds: Number(e.target.value) })}
            className="mt-2 w-full rounded-lg border border-border-light bg-bg px-3 py-2" /></label>
          <label className="text-sm">检索总时限（秒）<input type="number" min={10} max={120} value={draft.search_deadline_seconds}
            onChange={(e) => setDraft({ ...draft, search_deadline_seconds: Number(e.target.value) })}
            className="mt-2 w-full rounded-lg border border-border-light bg-bg px-3 py-2" /></label>
          <label className="text-sm">全文探测总时限（秒）<input type="number" min={0} max={120} value={draft.fulltext_verify_timeout_seconds}
            disabled={draft.paper_fetch_mode === "disabled"}
            onChange={(e) => setDraft({ ...draft, fulltext_verify_timeout_seconds: Number(e.target.value) })}
            className="mt-2 w-full rounded-lg border border-border-light bg-bg px-3 py-2 disabled:opacity-50" /></label>
          <div className="flex items-center justify-between rounded-lg border border-border-light px-3 py-2 text-sm">
            <span>检索时验证 OA 全文</span><AdminToggle checked={draft.verify_fulltext} disabled={draft.paper_fetch_mode === "disabled"}
              onChange={(next) => setDraft({ ...draft, verify_fulltext: next })} />
          </div>
        </div>
        <p className="mt-3 text-xs text-muted">熔断：连续 {data.breaker.threshold} 次明确失败后暂停 {data.breaker.cooldown_seconds} 秒，冷却后自动半开检测。</p>
      </AdminSection>

      <AdminSection title="连通性与真实检索检测" icon={<Activity className="h-5 w-5 text-accent" />}>
        <div className="mb-3 flex flex-wrap gap-2">
          {connectivityCatalog.map((source) => <label key={source.id} className="flex items-center gap-1.5 rounded-lg border border-border-light px-2.5 py-1.5 text-xs">
            <input type="checkbox" checked={connectSources.includes(source.id)} onChange={() => toggleChoice(connectSources, source.id, setConnectSources)} />{source.display_name}
          </label>)}
        </div>
        <button disabled={testing !== null || !connectSources.length} onClick={() => void runConnectivity()}
          className="flex h-9 items-center gap-2 rounded-lg bg-accent px-4 text-sm text-white disabled:opacity-50">
          {testing === "connectivity" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}开始检测
        </button>
        {latest?.connectivity && <DiagnosticTable run={latest.connectivity} kind="connectivity" />}
      </AdminSection>

      <AdminSection title="PDF 限量下载测速" icon={<Download className="h-5 w-5 text-accent" />}>
        <p className="mb-3 text-xs text-muted">只访问固定安全目标，最多读取 1 MiB、单项最多 20 秒；即使全文拉取已关闭，管理员仍可手动测速。</p>
        <div className="mb-3 flex flex-wrap gap-2">
          {downloadCatalog.map((source) => <label key={source.id} className="flex items-center gap-1.5 rounded-lg border border-border-light px-2.5 py-1.5 text-xs">
            <input type="checkbox" checked={downloadTargets.includes(source.id)} onChange={() => toggleChoice(downloadTargets, source.id, setDownloadTargets)} />{source.display_name}
          </label>)}
        </div>
        <button disabled={testing !== null || !downloadTargets.length} onClick={() => void runDownload()}
          className="flex h-9 items-center gap-2 rounded-lg bg-accent px-4 text-sm text-white disabled:opacity-50">
          {testing === "download" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}开始测速
        </button>
        {latest?.download && <DiagnosticTable run={latest.download} kind="download" />}
      </AdminSection>
    </div>

    <div className="sticky bottom-0 border-t border-border-light bg-bg/90 backdrop-blur">
      <div className="mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 py-3 sm:px-8">
        <span className="text-xs text-muted">版本 v{policy.version} · 保存后下一次检索立即生效</span>
        <div className="flex gap-2">
          {dirty && <button onClick={() => setDraft({ ...policy })} className="flex h-9 items-center gap-1.5 rounded-lg border border-border-light px-4 text-sm"><RotateCcw className="h-4 w-4" />重置</button>}
          <button disabled={!dirty || saving} onClick={() => void save()} className="flex h-9 items-center gap-1.5 rounded-lg bg-accent px-5 text-sm text-white disabled:opacity-50">
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}保存策略
          </button>
        </div>
      </div>
    </div>
    <HelpModal item={help} onClose={() => setHelp(null)} />
  </main>;
}

function DiagnosticTable({ run, kind }: { run: PaperDiagnosticRun; kind: "connectivity" | "download" }) {
  return <div className="mt-4 overflow-x-auto">
    <p className="mb-2 text-xs text-muted">最近完成：{new Date(run.finished_at * 1000).toLocaleString()}</p>
    <table className="w-full min-w-[760px] text-left text-xs">
      <thead className="border-b border-border-light text-muted"><tr>
        <th className="p-2">目标</th><th className="p-2">状态</th><th className="p-2">HTTP</th>
        <th className="p-2">耗时</th><th className="p-2">结果/字节</th><th className="p-2">PDF/速度</th><th className="p-2">异常</th>
      </tr></thead>
      <tbody>{run.items.map((item, index) => <tr key={`${String(item.source || item.target)}-${index}`} className="border-b border-border-light/60">
        <td className="p-2 font-medium">{String(item.source || item.target || "—")}</td>
        <td className="p-2">{statusText(item.status)}</td><td className="p-2">{String(item.http_status ?? "—")}</td>
        <td className="p-2">{String(item.latency_ms ?? item.elapsed_ms ?? "—")} ms</td>
        <td className="p-2">{kind === "connectivity" ? `${String(item.result_count ?? 0)} 篇` : `${String(item.bytes_read ?? 0)} B`}</td>
        <td className="p-2">{kind === "connectivity" ? statusText(item.pdf_probe_status) : `${String(item.kb_per_second ?? 0)} KB/s`}</td>
        <td className="p-2 text-muted">{String(item.error_code || "—")}</td>
      </tr>)}</tbody>
    </table>
  </div>;
}
