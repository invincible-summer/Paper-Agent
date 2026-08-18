"use client";

import { useEffect, useState } from "react";
import { AdminHeader, AdminSection, InfoButton, HelpModal, type HelpEntry } from "@/components/admin/AdminUI";
import { getPerformancePolicy, updatePerformancePolicy, type PerformancePolicyResponse, type StartupPrewarmMode, type MapCitationMode } from "@/lib/admin-api";

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

export default function PerformancePage() {
  const [data, setData] = useState<PerformancePolicyResponse | null>(null);
  const [warmup, setWarmup] = useState<StartupPrewarmMode>("blocking");
  const [citation, setCitation] = useState<MapCitationMode>("fast");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [helpItem, setHelpItem] = useState<HelpEntry | null>(null);
  const load = async () => { const d = await getPerformancePolicy(); setData(d); setWarmup(d.settings.startup_prewarm_mode); setCitation(d.settings.map_citation_mode); };
  useEffect(() => { load().catch(e => setMessage(String(e))); }, []);
  const save = async () => {
    if (!data) return;
    setBusy(true); setMessage("");
    try { const d = await updatePerformancePolicy(data.settings.version, { startup_prewarm_mode: warmup, map_citation_mode: citation }); setData(d); setMessage(d.restart_required ? "已保存；预热策略将在重启后生效。" : "已保存。"); }
    catch (e) { setMessage(String(e)); await load(); }
    finally { setBusy(false); }
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
    </div>
  </main>;
}
