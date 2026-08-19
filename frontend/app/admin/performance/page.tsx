"use client";

import { useEffect, useMemo, useState } from "react";
import { RotateCcw, Save, Timer, Zap } from "lucide-react";
import { AdminHeader, AdminSection, ConfirmModal, InfoButton, HelpModal, type HelpEntry } from "@/components/admin/AdminUI";
import {
  getPerformancePolicy, updatePerformancePolicy, getToolBudgets, updateToolBudgets, recoverToolBreaker,
  type PerformancePolicyResponse, type StartupPrewarmMode, type MapCitationMode,
  type ToolBudgetsResponse, type ToolBreakerState,
} from "@/lib/admin-api";

const warmupOptions: Array<[StartupPrewarmMode, string, string]> = [
  ["blocking", "阻塞预热（默认）", "服务就绪前导入 Agent，首个请求最快；就绪时间会增加约 9–12 秒。"],
  ["background", "后台预热", "先报告就绪，再后台导入；首个并发请求可能等待导入锁。"],
  ["role_first", "首帧优先", "不启动启动预热；/v1 先返回 role 帧，再在线程中加载 Agent。非流式请求仍会等待冷加载。"],
  ["off", "关闭", "保持冷启动行为，不主动导入 Agent。"],
];
const citationOptions: Array<[MapCitationMode, string, string]> = [
  ["fast", "快速（默认）", "后端批量 OpenAlex 引文增强最多等待 3 秒，失败仍返回地图。"],
  ["quality", "质量优先", "批量引文增强最多等待 8 秒，适合网络稳定且需要更多真实引用边的场景。"],
  ["off", "关闭", "不发起 OpenAlex 请求，地图保留聚类、时间线和语义边。"],
];

const BUDGET_HELP: HelpEntry = { title: "工具时限预算说明", entries: [
  ["生效规则", "每个工具的预算是单次调用的最长执行时间；实际生效值 = min(本预算, 整轮剩余时间 − 预留量)。超时后当前轮不会再调用该工具，下一轮自动恢复。"],
  ["清小搭 /v1 通道", "平台网关对整次请求的总超时是 120 秒，本服务因此整轮软时限 95 秒、硬时限 105 秒；预算 + 预留超过 95 秒的部分在 /v1 不会生效。"],
  ["Web 通道", "自制前端整轮 240/300 秒，较大的预算在 Web 端可以完整使用。"],
  ["预留量", "从整轮剩余时间中为最终答案生成与流式收尾预留的秒数，对所有工具生效；建议 8 秒。"],
  ["建议上限", "每个工具行内的「建议 ≤Xs」基于典型耗时给出；调大预算前请先在「论文检索」页确认检索内部时限与渠道连通状况。"],
  ["生效时机", "保存后对下一次工具调用立即生效，无需重启服务。"],
  ["工具熔断", "连续 3 次失败（含超时）的工具会被熔断 300 秒；行内显示状态并可在超时锁定时手动恢复。"],
] };

export default function PerformancePage() {
  const [data, setData] = useState<PerformancePolicyResponse | null>(null);
  const [warmup, setWarmup] = useState<StartupPrewarmMode>("blocking");
  const [citation, setCitation] = useState<MapCitationMode>("fast");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [helpItem, setHelpItem] = useState<HelpEntry | null>(null);

  const [budgets, setBudgets] = useState<ToolBudgetsResponse | null>(null);
  const [draftValues, setDraftValues] = useState<Record<string, number>>({});
  const [draftDefault, setDraftDefault] = useState(30);
  const [draftReserve, setDraftReserve] = useState(8);
  const [savingBudgets, setSavingBudgets] = useState(false);
  const [budgetMessage, setBudgetMessage] = useState("");
  const [recoverTool, setRecoverTool] = useState<string | null>(null);
  const [recovering, setRecovering] = useState("");

  const load = async () => { const d = await getPerformancePolicy(); setData(d); setWarmup(d.settings.startup_prewarm_mode); setCitation(d.settings.map_citation_mode); };
  const loadBudgets = async () => {
    const d = await getToolBudgets();
    setBudgets(d);
    setDraftValues(Object.fromEntries(d.catalog.map((c) => [c.name, c.current_seconds])));
    setDraftDefault(d.policy.default_budget_seconds);
    setDraftReserve(d.policy.reserve_seconds);
  };
  useEffect(() => {
    load().catch(e => setMessage(String(e)));
    loadBudgets().catch(e => setBudgetMessage(String(e)));
  }, []);
  const save = async () => {
    if (!data) return;
    setBusy(true); setMessage("");
    try { const d = await updatePerformancePolicy(data.settings.version, { startup_prewarm_mode: warmup, map_citation_mode: citation }); setData(d); setMessage(d.restart_required ? "已保存；预热策略将在重启后生效。" : "已保存。"); }
    catch (e) { setMessage(String(e)); await load(); }
    finally { setBusy(false); }
  };

  const budgetDirty = useMemo(() => {
    if (!budgets) return false;
    if (draftReserve !== budgets.policy.reserve_seconds) return true;
    if (draftDefault !== budgets.policy.default_budget_seconds) return true;
    return budgets.catalog.some((c) => (draftValues[c.name] ?? c.current_seconds) !== c.current_seconds);
  }, [budgets, draftValues, draftDefault, draftReserve]);

  const grouped = useMemo(() => {
    if (!budgets) return [] as Array<[string, ToolBudgetsResponse["catalog"]]>;
    const order: string[] = [];
    const map = new Map<string, ToolBudgetsResponse["catalog"]>();
    for (const item of budgets.catalog) {
      if (!map.has(item.category)) { map.set(item.category, []); order.push(item.category); }
      map.get(item.category)!.push(item);
    }
    return order.map((category) => [category, map.get(category)!] as const);
  }, [budgets]);

  const saveBudgets = async () => {
    if (!budgets) return;
    setSavingBudgets(true); setBudgetMessage("");
    try {
      const payload: Record<string, number> = {};
      for (const c of budgets.catalog) payload[c.name] = draftValues[c.name] ?? c.current_seconds;
      const d = await updateToolBudgets(budgets.policy.version, {
        budgets: payload, default_budget_seconds: draftDefault, reserve_seconds: draftReserve,
      });
      setBudgets(d);
      setDraftValues(Object.fromEntries(d.catalog.map((c) => [c.name, c.current_seconds])));
      setDraftDefault(d.policy.default_budget_seconds);
      setDraftReserve(d.policy.reserve_seconds);
      setBudgetMessage("工具时限预算已保存，下一次工具调用立即生效。");
    } catch (e) { setBudgetMessage(String(e)); await loadBudgets().catch(() => {}); }
    finally { setSavingBudgets(false); }
  };

  const resetBudgetDrafts = () => {
    if (!budgets) return;
    setDraftValues(Object.fromEntries(budgets.catalog.map((c) => [c.name, c.default_seconds])));
    setDraftDefault(30); setDraftReserve(8);
  };

  const doRecover = async (tool: string) => {
    setRecovering(tool);
    try {
      const r = await recoverToolBreaker(tool);
      setBudgets((old) => old ? { ...old, breaker_states: r.breaker_states } : old);
      setBudgetMessage(`已恢复 ${tool} 的工具熔断，下一次调用即可正常执行。`);
    } catch (e) { setBudgetMessage(String(e)); }
    finally { setRecovering(""); setRecoverTool(null); }
  };

  return <main className="mx-auto max-w-6xl space-y-6 p-6"><AdminHeader title="性能策略" subtitle="首帧、研究地图与工具预算" current="/admin/performance" /><HelpModal item={helpItem} onClose={() => setHelpItem(null)} />
    <div className="space-y-6 max-w-4xl">
      <AdminSection title="启动预热" info={<InfoButton onClick={() => setHelpItem({title: "启动预热说明", entries: warmupOptions.map(([v,l,h]) => [l,h])})} />}><p className="text-sm opacity-75 mt-1">只执行本地 Python 导入和 LLM 客户端构造，不调用 LLM/VLM、不下载模型、不增加外网带宽；blocking/background 会提前产生约 9–12 秒 CPU 峰值和约 290 MB 常驻内存。</p>
        <div className="mt-4 space-y-3">{warmupOptions.map(([value, label, help]) => <label key={value} className="flex items-start gap-3"><input type="radio" checked={warmup === value} onChange={() => setWarmup(value)} /><span><b>{label}</b><span className="block text-sm opacity-70">{help}</span></span></label>)}</div>
      </AdminSection>
      <AdminSection title="地图引文增强" info={<InfoButton onClick={() => setHelpItem({title: "地图引文说明", entries: citationOptions.map(([v,l,h]) => [l,h])})} />}><p className="text-sm opacity-75 mt-1">OpenAlex 请求由后端批量发起，只取元数据，不下载 PDF，不增加浏览器请求。模式立即生效。</p>
        <div className="mt-4 space-y-3">{citationOptions.map(([value, label, help]) => <label key={value} className="flex items-start gap-3"><input type="radio" checked={citation === value} onChange={() => setCitation(value)} disabled={!data?.openalex_enabled} /><span><b>{label}</b><span className="block text-sm opacity-70">{help}</span></span></label>)}</div>
        {data && !data.openalex_enabled && <p className="mt-3 text-amber-600">已禁用：{data.map_citation_disabled_reason}。有效模式为 off，保证零 OpenAlex 请求。</p>}
      </AdminSection>
      {data && <div className="rounded-xl bg-slate-50 dark:bg-slate-800 p-4 text-sm">当前进程：{data.prewarm.prewarm_state} · 模式 {data.prewarm.active_mode} · {Math.round(data.prewarm.duration_ms)} ms{data.prewarm.last_error && <span className="text-red-600"> · {data.prewarm.last_error}</span>}</div>}
      {message && <p className="text-sm">{message}</p>}
      <button disabled={busy || !data} onClick={save} className="rounded-lg bg-blue-600 px-5 py-2 text-white disabled:opacity-50">{busy ? "保存中…" : "保存性能策略"}</button>

      {budgets && <AdminSection title="工具时限预算" icon={<Timer className="h-5 w-5 text-accent" />}
        info={<InfoButton onClick={() => setHelpItem(BUDGET_HELP)} label="工具时限预算说明" />}>
        <p className="text-sm opacity-75">为每个工具单独设定单次调用的最长执行时间。实际生效值 = min(预算, 整轮剩余 − 预留量)；保存后立即生效，无需重启。把鼠标悬停在各行可查看建议上限的说明。</p>

        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <label className="rounded-xl border border-border-light p-4 text-sm" title="从整轮剩余时间中为最终答案生成与流式收尾预留的秒数，对所有工具生效。建议 8 秒；预留过小会导致工具挤占收尾时间，过大会压缩工具可用时长。">
            <span className="font-medium">整轮预留量（秒）</span>
            <span className="mt-1 block text-xs text-muted">为最终答案生成与流式收尾预留，对所有工具生效</span>
            <input type="number" min={budgets.limits.min_reserve} max={budgets.limits.max_reserve} value={draftReserve}
              onChange={(e) => setDraftReserve(Number(e.target.value))}
              className={`mt-2 w-32 rounded-lg border px-3 py-2 ${draftReserve !== budgets.policy.reserve_seconds ? "border-accent" : "border-border-light"} bg-bg`} />
            <span className="ml-2 text-xs text-muted">建议 8s</span>
          </label>
          <label className="rounded-xl border border-border-light p-4 text-sm" title="未在下方列出的工具（以及未来新增工具）使用的默认预算。">
            <span className="font-medium">默认预算（秒）</span>
            <span className="mt-1 block text-xs text-muted">未列出 / 未来新增工具使用的默认值</span>
            <input type="number" min={budgets.limits.min_seconds} max={budgets.limits.max_seconds} value={draftDefault}
              onChange={(e) => setDraftDefault(Number(e.target.value))}
              className={`mt-2 w-32 rounded-lg border px-3 py-2 ${draftDefault !== budgets.policy.default_budget_seconds ? "border-accent" : "border-border-light"} bg-bg`} />
            <span className="ml-2 text-xs text-muted">代码默认 30s</span>
          </label>
        </div>

        <div className="mt-5 space-y-5">
          {grouped.map(([category, items]) => <div key={category}>
            <h3 className="mb-2 text-sm font-semibold text-fg-secondary">{category}</h3>
            <div className="space-y-2">
              {items.map((item) => <ToolBudgetRow key={item.name} item={item}
                value={draftValues[item.name] ?? item.current_seconds}
                reserve={draftReserve} limits={budgets.limits}
                breaker={budgets.breaker_states[item.name]}
                onChange={(next) => setDraftValues((old) => ({ ...old, [item.name]: next }))}
                onRecover={() => setRecoverTool(item.name)} />)}
            </div>
          </div>)}
        </div>

        {budgetMessage && <p className="mt-4 text-sm">{budgetMessage}</p>}
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <button disabled={!budgetDirty || savingBudgets} onClick={() => void saveBudgets()}
            className="flex h-9 items-center gap-1.5 rounded-lg bg-accent px-5 text-sm text-white disabled:opacity-50">
            {savingBudgets ? "保存中…" : <><Save className="h-4 w-4" />保存工具时限</>}
          </button>
          {budgetDirty && <button onClick={resetBudgetDrafts}
            className="flex h-9 items-center gap-1.5 rounded-lg border border-border-light px-4 text-sm hover:bg-surface-hover">
            <RotateCcw className="h-4 w-4" />恢复代码默认值
          </button>}
          <span className="text-xs text-muted">版本 v{budgets.policy.version} · 保存后下一次工具调用立即生效</span>
        </div>
      </AdminSection>}
    </div>

    {recoverTool && budgets && (
      <ConfirmModal title="恢复工具熔断"
        body={`确认恢复「${budgets.catalog.find((c) => c.name === recoverTool)?.label || recoverTool}」（${recoverTool}）的工具熔断？恢复后下一次调用会立即重新执行；若故障未消除，连续失败会再次触发熔断。`}
        confirmLabel="立即恢复" busy={recovering === recoverTool}
        onConfirm={() => void doRecover(recoverTool)} onClose={() => setRecoverTool(null)} />
    )}
  </main>;
}

function ToolBudgetRow({ item, value, reserve, limits, breaker, onChange, onRecover }: {
  item: ToolBudgetsResponse["catalog"][number];
  value: number;
  reserve: number;
  limits: ToolBudgetsResponse["limits"];
  breaker?: ToolBreakerState;
  onChange: (next: number) => void;
  onRecover: () => void;
}) {
  const overRecommended = value > item.recommended_max;
  const exceedsApiTurn = value + reserve > limits.api_turn_soft_seconds;
  const edited = value !== item.current_seconds;
  return <div className={`flex flex-wrap items-center justify-between gap-3 rounded-xl border p-4 ${edited ? "border-accent/50" : "border-border-light"}`}>
    <div className="min-w-0 flex-1">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{item.label}</span>
        <code className="rounded bg-surface-hover px-1.5 py-0.5 text-[10px] text-muted">{item.name}</code>
        {item.overridden && <span className="rounded bg-accent/15 px-1.5 py-0.5 text-[10px] text-accent" title="当前值来自管理员覆盖，而非代码默认">已自定义</span>}
        {breaker?.state === "open" && <span className="flex items-center gap-1.5 rounded bg-error/15 px-1.5 py-0.5 text-[10px] text-error">
          已熔断 {Math.ceil(breaker.remaining_seconds)}s
          <button type="button" onClick={onRecover} title="立即清除熔断状态"
            className="flex items-center gap-1 rounded bg-error/10 px-1.5 py-0.5 hover:bg-error/20">
            <Zap className="h-3 w-3" />恢复
          </button>
        </span>}
        {breaker?.state === "half_open" && <span className="rounded bg-warning/15 px-1.5 py-0.5 text-[10px] text-warning">半开探测中</span>}
        {breaker && breaker.state === "closed" && breaker.consecutive_failures > 0
          && <span className="rounded bg-warning/10 px-1.5 py-0.5 text-[10px] text-warning">连续失败 {breaker.consecutive_failures}/3</span>}
      </div>
      <p className="mt-1 text-xs leading-relaxed text-muted">{item.description}</p>
      <p className="mt-1 text-xs text-fg-secondary" title={`建议不超过 ${item.recommended_max} 秒；代码默认 ${item.default_seconds} 秒。`}>
        建议 ≤{item.recommended_max}s · 代码默认 {item.default_seconds}s
        {value !== item.default_seconds && ` · 当前 ${value}s`}
      </p>
      {overRecommended && <p className="mt-1 text-xs text-amber-600">已超过建议上限 {item.recommended_max}s：请先确认网络与渠道状况，超时前无法完成的调用仍会作废。</p>}
      {exceedsApiTurn && <p className="mt-1 text-xs text-amber-600">预算 + 预留 &gt; /v1 整轮 {limits.api_turn_soft_seconds}s：超出部分仅 Web 通道（{limits.web_turn_soft_seconds}s）生效。</p>}
    </div>
    <label className="flex shrink-0 items-center gap-2 text-sm">
      <input type="number" min={limits.min_seconds} max={limits.max_seconds} value={Number.isFinite(value) ? value : ""}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-24 rounded-lg border border-border-light bg-bg px-3 py-2 text-right tabular-nums" />
      <span className="text-muted">秒</span>
    </label>
  </div>;
}
