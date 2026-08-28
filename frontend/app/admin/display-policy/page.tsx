"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  AlertTriangle, BookMarked, FileText, ImageIcon, LayoutGrid, ListChecks, Loader2, RotateCcw, Save,
} from "lucide-react";
import {
  AdminHeader, AdminSection, AdminToggle, HelpModal, InfoButton, type HelpEntry,
} from "@/components/admin/AdminUI";
import {
  AdminApiError, getDisplayPolicy, updateDisplayPolicy,
  type ApiDisplayPolicy, type BibtexExportMode,
} from "@/lib/admin-api";
import { useAuthStore } from "@/stores/auth";

const BIBTEX_EXPORT_MODES: Array<{ key: BibtexExportMode; name: string; hint: string }> = [
  {
    key: "bib_and_md",
    name: "同时导出 .bib 与 .md",
    hint: "保留 .bib 附件，并额外附一份内容完全相同的 .md 副本。",
  },
  {
    key: "md_only",
    name: "仅导出 .md",
    hint: "只附 .md 文件（默认）。内容与 .bib 逐字节一致，改回 .bib 扩展名即可使用。",
  },
  {
    key: "bib_only",
    name: "仅导出 .bib",
    hint: "历史行为：只生成 .bib 附件，清小搭侧无法下载，且不追加末行说明。",
  },
];

const HELP: Record<string, HelpEntry> = {
  toolCards: {
    title: "工具状态行是什么",
    entries: [
      ["哪里会显示", "每个工具完成时，回复正文顶部出现一行状态摘要，例如「🔎 文献检索 · 核心集 12 篇 / 候选 13 篇」；思考折叠中的进度提示与文件附件不受此开关影响。"],
      ["这个开关控制什么", "只控制正文里的工具状态行。关闭后正文更干净，工具进度仍在思考折叠中可见。"],
      ["检索结果表格", "文献检索完成后，正文中的完整论文清单表格（核心集 / 候选集、全文可取性、原文链接）是规范化输出，不受此开关控制，始终生成。"],
      ["对自制前端的影响", "无。此策略只作用于清小搭 /v1 通道的输出；自制前端始终显示原生 React 卡片。"],
      ["生效时间", "保存后立即生效，下一条 /v1 消息即按新策略渲染；已经发出的历史消息不变。"],
    ],
  },
  toolErrors: {
    title: "工具错误提示是什么",
    entries: [
      ["哪里会显示", "工具出错或超时时，回复正文顶部出现一行「⚠️ 🔎 文献检索 · 工具 search_papers 本轮已开始执行过一次…」之类的错误摘要。"],
      ["这个开关控制什么", "只控制正文里的错误状态行。默认关闭：正式输出不显示任何 ⚠️ 工具错误提示，正文更干净。"],
      ["关闭后错误去哪了", "错误信息仍会完整交给模型处理，模型会在回答文字中自行说明；超时、防重复调用等控制逻辑完全不变，只是不再展示给最终用户。"],
      ["思考折叠受影响吗", "不受影响。工具进度、技能加载等思考折叠里的提示始终保留。"],
      ["什么时候打开它", "排查问题时可临时打开，观察每个工具的失败原因；日常使用建议保持关闭。"],
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
  researchMap: {
    title: "研究图谱组合输出",
    entries: [
      ["美化 SVG 附件", "自包含 image/svg+xml 矢量图，可无损缩放，不依赖 Mermaid、JavaScript、外部字体或远程资源。"],
      ["正文 Mermaid", "实验性正文代码块；清小搭手册不承诺原生 Mermaid 执行，不支持时会显示源码。预算不足时整块省略。"],
      ["可执行 HTML", "自包含 HTML 下载附件，提供筛选、平移缩放、边切换、详情和聚合节点展开；下载后在浏览器中执行。"],
      ["附加 Markdown 说明", "独立的普通关系说明附件，包含主题摘要、论文、DOI/来源、引用边和聚合成员，不嵌入 Mermaid。"],
      ["生效范围", "只影响保存后新生成的 /v1 研究地图；历史附件与自有 Web 前端的 GenealogyGraph 均不改变。"],
    ],
  },
  bibtexExport: {
    title: "BibTeX 导出格式",
    entries: [
      ["为什么需要这个设置", "清小搭侧无法下载 .bib 格式的附件文件，参考文献导出需要借助 .md 格式传递。"],
      [".md 文件内容", "与 .bib 文件逐字节一致的原始 BibTeX 文本：下载后把扩展名从 .md 改成 .bib 即可直接使用。"],
      ["末行说明", "只要本轮借助 .md 导出（即模式不是「仅导出 .bib」），正式输出的最后一行会提示用户手动转存为 .bib；该说明行不受「工具状态行」开关控制。"],
      ["影响范围", "只作用于清小搭 /v1 通道的 BibTeX 参考文献导出；GB/T 7714（.txt）与自有 Web 前端的 .bib 下载不受影响。"],
      ["生效时间", "保存后立即生效，下一条含参考文献导出的 /v1 消息即按新模式生成附件。"],
    ],
  },
  save: {
    title: "保存与版本说明",
    entries: [
      ["乐观锁", "策略带版本号。若其他管理员刚保存过，本页保存会返回 409 冲突——刷新页面拿到最新版本后再修改。"],
      ["重置修改", "放弃当前未保存的改动，回到上次保存（或服务器上）的策略。"],
      ["默认值", "首次部署默认开启工具状态行、技能行和美化 SVG；工具错误提示行、Mermaid、HTML、Markdown 默认关闭；BibTeX 导出格式默认「仅导出 .md」。旧配置会按原有语义迁移。"],
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
    if (draft.tool_cards_enabled !== policy.tool_cards_enabled) {
      out.tool_cards_enabled = draft.tool_cards_enabled;
    }
    if (draft.tool_error_cards_enabled !== policy.tool_error_cards_enabled) {
      out.tool_error_cards_enabled = draft.tool_error_cards_enabled;
    }
    if (draft.skill_card_enabled !== policy.skill_card_enabled) {
      out.skill_card_enabled = draft.skill_card_enabled;
    }
    for (const key of [
      "research_map_svg_enabled", "research_map_mermaid_enabled",
      "research_map_html_enabled", "research_map_markdown_enabled",
    ] as const) {
      if (draft[key] !== policy[key]) out[key] = draft[key];
    }
    if (draft.bibtex_export_mode !== policy.bibtex_export_mode) {
      out.bibtex_export_mode = draft.bibtex_export_mode;
    }
    return out;
  }, [policy, draft]);

  const dirty = Object.keys(changes).length > 0;

  const setMapToggle = (
    key: "research_map_svg_enabled" | "research_map_mermaid_enabled"
      | "research_map_html_enabled" | "research_map_markdown_enabled",
    next: boolean,
  ) => {
    if (!draft) return;
    const candidate = { ...draft, [key]: next };
    if (!candidate.research_map_svg_enabled && !candidate.research_map_mermaid_enabled
        && !candidate.research_map_html_enabled) {
      setError("美化 SVG、正文 Mermaid、可执行 HTML 至少保留一种。");
      return;
    }
    setError("");
    setDraft(candidate);
  };

  const save = async () => {
    if (!policy || !dirty) return;
    setWorking(true); setError(""); setSaved(false);
    try {
      const result = await updateDisplayPolicy(policy.version, changes);
      setPolicy(result.policy); setDraft(result.policy);
      setSaved(true);
    } catch (err) { handleError(err); } finally { setWorking(false); }
  };

  if (!checked || loading) return <div className="flex min-h-[60vh] items-center justify-center"><Loader2 className="h-6 w-6 animate-spin text-accent" /></div>;
  if (!user || user.role !== "administrator") return <div className="py-16 text-center text-muted">仅管理员可访问。</div>;
  if (!draft || !policy) return <div className="py-16 text-center text-error">{error || "策略加载失败"}</div>;

  return <>
    <div className="space-y-6 pb-24">
      <AdminHeader title="清小搭展示策略" icon={<LayoutGrid className="h-5 w-5" />}
        subtitle="控制 /v1 通道的状态行、技能提示与引用关系图谱附件；不影响自有前端。"
        onRefresh={() => void refresh()} refreshing={loading} />

      {error && <div className="rounded-lg border border-error/40 bg-error/10 p-3 text-sm text-error">{error}</div>}
      {saved && !dirty && <div className="rounded-lg border border-success/40 bg-success/10 p-3 text-sm text-success">策略已保存，下一条 /v1 消息立即生效。</div>}

      <AdminSection title="工具状态行" icon={<ListChecks className="h-5 w-5 text-accent" />}
        info={<InfoButton onClick={() => setHelpItem(HELP.toolCards)} label="工具状态行说明" />}>
        <div className="flex items-center justify-between gap-4">
          <p className="text-sm text-muted">
            每个工具完成时在回复正文顶部显示一行状态摘要（如「🔎 文献检索 · 核心集 12 篇 / 候选 13 篇」）；
            思考折叠中的进度提示与文件附件始终保留，检索结果的完整论文表格不受此开关控制。
          </p>
          <AdminToggle checked={draft.tool_cards_enabled} label="工具状态行"
            onChange={(next) => setDraft({ ...draft, tool_cards_enabled: next })} />
        </div>
      </AdminSection>

      <AdminSection title="工具错误提示" icon={<AlertTriangle className="h-5 w-5 text-warning" />}
        info={<InfoButton onClick={() => setHelpItem(HELP.toolErrors)} label="工具错误提示说明" />}>
        <div className="flex items-center justify-between gap-4">
          <p className="text-sm text-muted">
            工具出错或超时时在正文顶部显示「⚠️ …」错误摘要行。默认关闭：正式输出不显示任何工具错误提示；
            错误仍会交给模型处理并在回答文字中说明，超时与调用控制逻辑不变。
          </p>
          <AdminToggle checked={draft.tool_error_cards_enabled} label="错误状态卡片"
            onChange={(next) => setDraft({ ...draft, tool_error_cards_enabled: next })} />
        </div>
      </AdminSection>

      <AdminSection title="技能加载提示" icon={<BookMarked className="h-5 w-5 text-accent" />}
        info={<InfoButton onClick={() => setHelpItem(HELP.skill)} label="技能加载提示说明" />}>
        <div className="flex items-center justify-between gap-4">
          <p className="text-sm text-muted">
            技能加载时在回复正文顶部显示「━━ 📘 技能 · 名称 ━━」提示行；思考折叠中的提示始终保留。
          </p>
          <AdminToggle checked={draft.skill_card_enabled} label="正文技能提示行"
            onChange={(next) => setDraft({ ...draft, skill_card_enabled: next })} />
        </div>
      </AdminSection>

      <AdminSection title="引用关系图谱组合输出" icon={<ImageIcon className="h-5 w-5 text-accent" />}
        info={<InfoButton onClick={() => setHelpItem(HELP.researchMap)} label="图谱组合输出说明" />}>
        <div className="space-y-4">
          <p className="text-sm text-muted">
            四个开关彼此独立。美化 SVG、正文 Mermaid、可执行 HTML 中至少启用一种；Markdown
            是可单独关闭的附加说明，不承担唯一图形展示职责。
          </p>
          <div className="grid gap-3 sm:grid-cols-2">
            <AdminToggle checked={draft.research_map_svg_enabled} label="美化 SVG 附件"
              onChange={(next) => setMapToggle("research_map_svg_enabled", next)} />
            <AdminToggle checked={draft.research_map_mermaid_enabled} label="正文 Mermaid（实验性）"
              onChange={(next) => setMapToggle("research_map_mermaid_enabled", next)} />
            <AdminToggle checked={draft.research_map_html_enabled} label="可执行 HTML 附件"
              onChange={(next) => setMapToggle("research_map_html_enabled", next)} />
            <AdminToggle checked={draft.research_map_markdown_enabled} label="附加 Markdown 说明"
              onChange={(next) => setMapToggle("research_map_markdown_enabled", next)} />
          </div>
          <div className="grid gap-2 text-xs leading-5 text-muted sm:grid-cols-2">
            <p>SVG：自包含矢量图；Mermaid：不支持原生执行时显示源码，预算不足会整块省略。</p>
            <p>HTML：清小搭提供下载卡片，下载后在浏览器执行，不在主站同源页面内联。</p>
            <p>Markdown：普通关系清单，不嵌 Mermaid，可复制论文、DOI/来源和聚合成员。</p>
            <p>保存后从下一轮 /v1 请求开始生效；历史附件与 Web 端 GenealogyGraph 不变。</p>
          </div>
        </div>
      </AdminSection>

      <AdminSection title="BibTeX 导出格式" icon={<FileText className="h-5 w-5 text-accent" />}
        info={<InfoButton onClick={() => setHelpItem(HELP.bibtexExport)} label="BibTeX 导出格式说明" />}>
        <div className="space-y-3">
          <p className="text-sm text-muted">
            清小搭侧无法下载 .bib 格式附件，参考文献导出可借助 .md 格式传递；.md 文件内容与
            .bib 完全一致，改回扩展名即可使用。GB/T 7714（.txt）与自有 Web 前端不受此设置影响。
          </p>
          <div className="grid gap-2 sm:grid-cols-3" role="radiogroup" aria-label="BibTeX 导出格式">
            {BIBTEX_EXPORT_MODES.map(({ key, name }) => {
              const active = draft.bibtex_export_mode === key;
              return (
                <button key={key} type="button" role="radio" aria-checked={active}
                  onClick={() => setDraft({ ...draft, bibtex_export_mode: key })}
                  className={`h-10 rounded-lg border px-3 text-sm transition-colors ${
                    active ? "border-accent bg-accent/10 font-medium text-accent"
                      : "border-border-light text-fg-secondary hover:bg-surface-hover hover:text-fg"}`}>
                  {name}
                </button>
              );
            })}
          </div>
          <p className="text-xs leading-5 text-muted">
            {BIBTEX_EXPORT_MODES.find((m) => m.key === draft.bibtex_export_mode)?.hint}
            {" "}模式不为「仅导出 .bib」时，正式输出最后一行会提示用户手动转存为 .bib。
          </p>
        </div>
      </AdminSection>
    </div>

    <div className="sticky bottom-0 -mx-4 border-t border-border-light bg-bg/90 px-4 backdrop-blur sm:-mx-8 sm:px-8">
      <div className="flex flex-wrap items-center justify-between gap-3 py-3">
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
  </>;
}
