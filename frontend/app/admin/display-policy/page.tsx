"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  BookMarked, Check, LayoutGrid, Loader2, RotateCcw, Save,
} from "lucide-react";
import {
  AdminHeader, AdminSection, HelpModal, InfoButton, type HelpEntry,
} from "@/components/admin/AdminUI";
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

const PRESETS: Array<{
  key: ApiDisplayPolicy["preset"];
  name: string;
  desc: string;
}> = [
  { key: "core", name: "重点工具完整卡片", desc: "检索、研究地图、阅读路径、深读、元素解读、领域普查、综述、引文导出显示完整卡片，其余一行摘要。" },
  { key: "all", name: "全部工具完整卡片", desc: "所有工具都渲染完整 Markdown 卡片；消息更长，与模型回答的内容重复更多。" },
  { key: "custom", name: "自定义选择", desc: "在下方勾选需要完整卡片的工具；未勾选的工具显示一行摘要。" },
  { key: "off", name: "关闭卡片", desc: "不输出任何 Markdown 卡片，仅保留思考折叠中的进度提示与文件附件。" },
];

const HELP: Record<string, HelpEntry> = {
  presets: {
    title: "四种卡片预设有什么区别？",
    entries: [
      ["重点工具完整卡片（推荐默认）", "只有 8 个信息量最大的工具（文献检索 / 研究地图 / 阅读路径 / 深度阅读 / 元素解读 / 领域普查 / 文献综述 / 参考文献导出）渲染完整卡片；其余工具显示一行摘要，消息干净且关键信息不丢。"],
      ["全部工具完整卡片", "16 个工具都渲染完整卡片，最接近自制前端的密度；消息明显更长，且与模型自己的总结重复较多。"],
      ["自定义选择", "逐个勾选哪些工具出完整卡片，未勾选的一律一行摘要。适合想突出某些工具、隐藏其余卡片的场景。"],
      ["关闭卡片", "不输出任何 Markdown 卡片。工具进度提示（思考折叠里）和文件附件（图谱 SVG、裁剪图、BibTeX 等）不受影响，仍正常下发。"],
      ["对自制前端的影响", "无。此策略只作用于清小搭 /v1 通道的输出；自制前端始终显示原生 React 卡片。"],
      ["生效时间", "保存后立即生效，下一条 /v1 消息即按新策略渲染；已经发出的历史消息不变。"],
    ],
  },
  custom: {
    title: "完整卡片与一行摘要的区别",
    entries: [
      ["完整卡片", "结构化 Markdown 块：标题行（图标 + 统计数字）+ 明细列表。例如文献检索列出每篇论文的年份 / 被引 / 全文状态徽章；元素解读展示图表描述、表格或 LaTeX 公式。"],
      ["一行摘要", "单行结论，例如「🛡️ 可靠性质检 · 12 篇已核验：无异常 10 · 撤稿 1」。适合低信息量工具或想让消息更简洁时。"],
      ["勾选与预设的关系", "只有在预设为「自定义选择」时，勾选列表才生效；切换到其他预设后勾选仍保留，便于来回切换。"],
      ["附件不受影响", "无论选哪种预设，文件附件（研究地图 SVG、裁剪图 PNG、BibTeX、趋势图）都会正常生成和下发——预设只控制正文里的 Markdown 卡片。"],
    ],
  },
  skill: {
    title: "技能加载提示是什么",
    entries: [
      ["哪里会显示", "模型加载一个工作流技能（如「文献综述写作」「研究空白识别」）时：思考折叠里出现「📘 已加载技能《…》，按其工作流执行」；回复正文顶部出现一行「━━ 📘 技能 · 名称 ━━」。"],
      ["这个开关控制什么", "只控制正文里的技能行。思考折叠中的提示始终保留（它属于进度信息，不占正文）。"],
      ["什么时候关掉它", "如果觉得正文里的技能行打扰阅读，可以关闭；关闭后管理员仍能在思考折叠中看到技能加载过程。"],
      ["与自制前端的区别", "自制前端不显示技能加载（技能是内部指令加载）；此提示是清小搭通道专用的可视化。"],
    ],
  },
  save: {
    title: "保存与版本说明",
    entries: [
      ["乐观锁", "策略带版本号。若其他管理员刚保存过，本页保存会返回 409 冲突——刷新页面拿到最新版本后再修改。"],
      ["重置修改", "放弃当前未保存的改动，回到上次保存（或服务器上）的策略。"],
      ["默认值", "首次部署的默认策略是「重点工具完整卡片」+ 技能行开启，无需任何配置即有完整效果。"],
    ],
  },
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
  const [helpItem, setHelpItem] = useState<HelpEntry | null>(null);
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
    setLoading(true); setError(""); setSaved(false);
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

  const dirty = Object.keys(changes).length > 0;

  const save = async () => {
    if (!policy || !dirty) return;
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
    <div className="mx-auto max-w-4xl space-y-6 pb-24">
      <AdminHeader title="清小搭卡片展示策略" icon={<LayoutGrid className="h-5 w-5" />}
        subtitle="控制 /v1 通道在清小搭上的 Markdown 卡片与技能提示；不影响自制前端。"
        current="/admin/display-policy" onRefresh={() => void refresh()} refreshing={loading} />

      {error && <div className="rounded-lg border border-red-400/40 bg-red-500/10 p-3 text-sm text-red-500">{error}</div>}
      {saved && !dirty && <div className="rounded-lg border border-emerald-400/40 bg-emerald-500/10 p-3 text-sm text-emerald-600">策略已保存，下一条 /v1 消息立即生效。</div>}

      <AdminSection title="卡片预设" info={<InfoButton onClick={() => setHelpItem(HELP.presets)} label="四种预设的区别" />}>
        <div className="grid gap-3 sm:grid-cols-2">
          {PRESETS.map(({ key, name, desc }) => {
            const active = draft.preset === key;
            return (
              <button key={key} onClick={() => setDraft({ ...draft, preset: key })}
                aria-pressed={active}
                className={`flex min-h-28 flex-col rounded-xl border p-4 text-left transition-colors ${
                  active ? "border-accent bg-accent/10" : "border-border-light hover:bg-surface-hover"}`}>
                <div className="flex items-center justify-between gap-2">
                  <span className={`font-medium ${active ? "text-accent" : ""}`}>
                    {key === "core" ? `${name}（推荐）` : name}
                  </span>
                  {active && <Check className="h-4 w-4 shrink-0 text-accent" aria-label="已选中" />}
                </div>
                <p className="mt-1.5 text-xs leading-relaxed text-muted">{desc}</p>
              </button>
            );
          })}
        </div>
      </AdminSection>

      {draft.preset === "custom" && (
        <AdminSection title="完整卡片工具"
          info={<InfoButton onClick={() => setHelpItem(HELP.custom)} label="完整卡片与一行摘要的区别" />}>
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <span className="text-sm text-muted">已选 {draft.enabled_tools.length} / {tools.length} 个工具</span>
            <div className="flex gap-2">
              <button onClick={() => setDraft({ ...draft, enabled_tools: [...coreTools] })}
                className="h-8 rounded-lg border border-border-light px-3 text-xs text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg">全选重点</button>
              <button onClick={() => setDraft({ ...draft, enabled_tools: [...tools] })}
                className="h-8 rounded-lg border border-border-light px-3 text-xs text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg">全选</button>
              <button onClick={() => setDraft({ ...draft, enabled_tools: [] })}
                className="h-8 rounded-lg border border-border-light px-3 text-xs text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg">清空</button>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            {tools.map((tool) => {
              const selected = draft.enabled_tools.includes(tool);
              return (
                <label key={tool} aria-pressed={selected}
                  className={`flex min-h-11 cursor-pointer items-center gap-2.5 rounded-lg border px-3 text-sm transition-colors ${
                    selected ? "border-accent bg-accent/10" : "border-border-light hover:bg-surface-hover"}`}>
                  <input type="checkbox" checked={selected} onChange={() => toggleTool(tool)}
                    aria-label={TOOL_LABELS[tool] || tool}
                    className="h-4 w-4 shrink-0 accent-[rgb(var(--accent))]" />
                  <span className="truncate">{TOOL_LABELS[tool] || tool}</span>
                  {coreTools.includes(tool) && (
                    <span className="ml-auto shrink-0 rounded bg-accent/20 px-1.5 py-0.5 text-[10px] text-accent">重点</span>
                  )}
                </label>
              );
            })}
          </div>
        </AdminSection>
      )}

      <AdminSection title="技能加载提示" icon={<BookMarked className="h-5 w-5 text-accent" />}
        info={<InfoButton onClick={() => setHelpItem(HELP.skill)} label="技能加载提示说明" />}>
        <div className="flex items-center justify-between gap-4">
          <p className="text-sm text-muted">
            技能加载时在回复正文顶部显示「━━ 📘 技能 · 名称 ━━」提示行；思考折叠中的提示始终保留。
          </p>
          <button type="button" role="switch" aria-checked={draft.skill_card_enabled}
            aria-label="正文技能提示行"
            onClick={() => setDraft({ ...draft, skill_card_enabled: !draft.skill_card_enabled })}
            className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${
              draft.skill_card_enabled ? "bg-accent" : "border border-border-light bg-surface-hover"}`}>
            <span className={`absolute top-0.5 h-5 w-5 rounded-full shadow transition-all ${
              draft.skill_card_enabled ? "left-[22px]" : "left-0.5 bg-muted/60"}`} />
          </button>
        </div>
      </AdminSection>
    </div>

    <div className="sticky bottom-0 border-t border-border-light bg-bg/90 backdrop-blur">
      <div className="mx-auto flex max-w-4xl flex-wrap items-center justify-between gap-3 px-4 py-3 sm:px-8">
        <div className="flex items-center gap-2 text-xs text-muted">
          <InfoButton onClick={() => setHelpItem(HELP.save)} label="保存与版本说明" />
          <span>版本 v{policy.version} · 上次由 {policy.updated_by} 更新 · 保存后立即生效</span>
        </div>
        <div className="flex gap-2">
          {dirty && (
            <button onClick={() => setDraft({ ...policy })}
              className="flex h-9 items-center gap-1.5 rounded-lg border border-border-light px-4 text-sm text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg">
              <RotateCcw className="h-4 w-4" />重置修改
            </button>
          )}
          <button disabled={working || !dirty} onClick={() => void save()}
            className="flex h-9 items-center gap-1.5 rounded-lg bg-accent px-5 text-sm font-medium text-white transition-colors hover:bg-accent-hover disabled:opacity-50">
            {working ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            保存策略
          </button>
        </div>
      </div>
    </div>

    <HelpModal item={helpItem} onClose={() => setHelpItem(null)} />
  </main>;
}
