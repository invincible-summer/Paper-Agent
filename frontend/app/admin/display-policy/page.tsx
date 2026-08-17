"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowLeft, BookMarked, Database, KeyRound, Loader2, RefreshCw, Save } from "lucide-react";
import {
  AdminApiError, getDisplayPolicy, updateDisplayPolicy,
  type ApiDisplayPolicy,
} from "@/lib/admin-api";
import { useAuthStore } from "@/stores/auth";

const TOOL_LABELS: Record<string, string> = {
  search_papers: "文献检索", deep_read: "深度阅读", ask_papers: "论文问答",
  research_map: "研究地图", reading_path: "阅读路径", write_review: "文献综述",
  citation_export: "参考文献导出", export_report: "报告导出",
  check_structure: "结构体检", check_format: "格式检查",
  export_manuscript: "文稿导出", integrity_sweep: "可靠性质检",
  bib_import: "文献库导入", exhibit_index: "图表导览",
  explain_element: "元素解读", field_census: "领域普查",
};

const PRESET_DESC: Record<ApiDisplayPolicy["preset"], string> = {
  core: "重点工具完整卡片（默认）：检索、研究地图、阅读路径、深读、元素解读、领域普查、综述、引文导出显示完整卡片，其余工具一行摘要。",
  all: "全部工具完整卡片：所有工具都渲染完整 Markdown 卡片，消息更长、与回答内容重复更多。",
  custom: "自定义：勾选需要完整卡片的工具，未勾选的工具显示一行摘要。",
  off: "关闭卡片：不输出任何 Markdown 卡片，仅保留思考折叠中的进度提示与文件附件。",
};

export default function DisplayPolicyAdminPage() {
  const router = useRouter();
  const checked = useAuthStore((s) => s.checked);
  const user = useAuthStore((s) => s.user);
  const signOut = useAuthStore((s) => s.signOut);
  const [policy, setPolicy] = useState<ApiDisplayPolicy | null>(null);
  const [draft, setDraft] = useState<ApiDisplayPolicy | null>(null);
  const [tools, setTools] = useState<string[]>([]);
  const [coreTools, setCoreTools] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => { void useAuthStore.getState().hydrate(); }, []);
  const handleError = useCallback((err: unknown) => {
    if (err instanceof AdminApiError && err.status === 401) { signOut(); router.replace("/login"); return; }
    setError(err instanceof Error ? err.message : "请求失败");
  }, [router, signOut]);

  const refresh = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const data = await getDisplayPolicy();
      setPolicy(data.policy); setDraft(data.policy);
      setTools(data.tools); setCoreTools(data.core_tools);
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
    const out: Partial<ApiDisplayPolicy> = {};
    if (draft.preset !== policy.preset) out.preset = draft.preset;
    if (draft.skill_card_enabled !== policy.skill_card_enabled) {
      out.skill_card_enabled = draft.skill_card_enabled;
    }
    if (draft.preset === "custom" && (
      draft.preset !== policy.preset
      || JSON.stringify([...draft.enabled_tools].sort())
        !== JSON.stringify([...policy.enabled_tools].sort())
    )) out.enabled_tools = [...draft.enabled_tools].sort();
    return out;
  }, [policy, draft]);

  const save = async () => {
    if (!policy || !Object.keys(changes).length) return;
    setWorking(true); setError(""); setSaved(false);
    try {
      const result = await updateDisplayPolicy(policy.version, changes);
      setPolicy(result.policy); setDraft(result.policy);
      setSaved(true);
    } catch (err) { handleError(err); } finally { setWorking(false); }
  };

  const toggleTool = (tool: string) => {
    if (!draft) return;
    const selected = new Set(draft.enabled_tools);
    if (selected.has(tool)) selected.delete(tool);
    else selected.add(tool);
    setDraft({ ...draft, enabled_tools: [...selected].sort() });
  };

  if (!checked || loading) return <div className="flex min-h-screen items-center justify-center"><Loader2 className="h-6 w-6 animate-spin text-accent" /></div>;
  if (!user || user.role !== "administrator") return <div className="p-8 text-center text-muted">仅管理员可访问。</div>;
  if (!draft || !policy) return <div className="p-8 text-center text-red-500">{error || "策略加载失败"}</div>;

  return <main className="min-h-screen bg-bg px-4 py-8 text-fg sm:px-8">
    <div className="mx-auto max-w-4xl space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <button onClick={() => router.push("/chat")} className="rounded-lg border border-border-light p-2"><ArrowLeft className="h-4 w-4" /></button>
          <div>
            <h1 className="text-2xl font-bold">清小搭卡片展示策略</h1>
            <p className="text-sm text-muted">控制 /v1 通道在清小搭上的 Markdown 卡片仿真与技能提示，不影响自制前端。</p>
          </div>
        </div>
        <div className="flex gap-2">
          <button onClick={() => router.push("/admin/api-storage")} className="flex items-center gap-2 rounded-lg border border-border-light px-3 py-2 text-sm"><Database className="h-4 w-4" />API 存储</button>
          <button onClick={() => router.push("/admin/agent-keys")} className="flex items-center gap-2 rounded-lg border border-border-light px-3 py-2 text-sm"><KeyRound className="h-4 w-4" />Agent Keys</button>
          <button onClick={() => void refresh()} className="rounded-lg border border-border-light p-2"><RefreshCw className="h-4 w-4" /></button>
        </div>
      </header>
      {error && <div className="rounded-lg border border-red-400/40 bg-red-500/10 p-3 text-sm text-red-500">{error}</div>}
      {saved && <div className="rounded-lg border border-emerald-400/40 bg-emerald-500/10 p-3 text-sm text-emerald-600">策略已保存，下一条 /v1 消息立即生效。</div>}

      <section className="rounded-xl border border-border-light bg-surface p-5">
        <h2 className="mb-3 font-semibold">卡片预设</h2>
        <div className="grid gap-3 sm:grid-cols-2">
          {(["core", "all", "custom", "off"] as const).map((name) => (
            <button key={name} onClick={() => setDraft({ ...draft, preset: name })}
              className={`rounded-lg border p-4 text-left ${draft.preset === name ? "border-accent bg-accent/10" : "border-border-light"}`}>
              <div className="font-medium">{{ core: "重点工具完整卡片（推荐）", all: "全部工具完整卡片", custom: "自定义选择", off: "关闭卡片" }[name]}</div>
              <div className="mt-1 text-xs text-muted">{PRESET_DESC[name]}</div>
            </button>
          ))}
        </div>
      </section>

      {draft.preset === "custom" && (
        <section className="rounded-xl border border-border-light bg-surface p-5">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="font-semibold">完整卡片工具</h2>
            <div className="flex gap-2 text-sm">
              <button onClick={() => setDraft({ ...draft, enabled_tools: [...coreTools] })} className="rounded border border-border-light px-3 py-1">全选重点</button>
              <button onClick={() => setDraft({ ...draft, enabled_tools: [...tools] })} className="rounded border border-border-light px-3 py-1">全选</button>
              <button onClick={() => setDraft({ ...draft, enabled_tools: [] })} className="rounded border border-border-light px-3 py-1">清空</button>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            {tools.map((tool) => (
              <label key={tool} className={`flex cursor-pointer items-center gap-2 rounded border p-2 text-sm ${draft.enabled_tools.includes(tool) ? "border-accent bg-accent/10" : "border-border-light"}`}>
                <input type="checkbox" checked={draft.enabled_tools.includes(tool)} onChange={() => toggleTool(tool)} className="accent-[var(--accent)]" />
                <span>{TOOL_LABELS[tool] || tool}</span>
                {coreTools.includes(tool) && <span className="ml-auto rounded bg-accent/20 px-1.5 py-0.5 text-[10px] text-accent">重点</span>}
              </label>
            ))}
          </div>
        </section>
      )}

      <section className="rounded-xl border border-border-light bg-surface p-5">
        <div className="mb-2 flex items-center gap-2"><BookMarked className="h-5 w-5 text-accent" /><h2 className="font-semibold">技能加载提示</h2></div>
        <label className="flex cursor-pointer items-center justify-between gap-3 text-sm">
          <span>技能加载时在回复正文显示「━━ 📘 技能 · 名称 ━━」提示行（思考折叠中的提示始终保留）</span>
          <input type="checkbox" checked={draft.skill_card_enabled} onChange={() => setDraft({ ...draft, skill_card_enabled: !draft.skill_card_enabled })} className="h-5 w-5 accent-[var(--accent)]" />
        </label>
      </section>

      <div className="flex items-center gap-3">
        <button disabled={working || !Object.keys(changes).length} onClick={() => void save()}
          className="flex items-center gap-2 rounded-lg bg-accent px-5 py-2 text-sm font-medium text-white disabled:opacity-50">
          <Save className="h-4 w-4" />保存策略
        </button>
        <span className="text-sm text-muted">版本 {policy.version} · 上次由 {policy.updated_by} 更新</span>
      </div>
    </div>
  </main>;
}
