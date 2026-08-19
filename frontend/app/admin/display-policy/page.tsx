"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  BookMarked, ImageIcon, LayoutGrid, ListChecks, Loader2, RotateCcw, Save,
} from "lucide-react";
import {
  AdminHeader, AdminSection, AdminToggle, HelpModal, InfoButton, type HelpEntry,
} from "@/components/admin/AdminUI";
import {
  AdminApiError, getDisplayPolicy, updateDisplayPolicy,
  type ApiDisplayPolicy, type ResearchMapRenderStrategy,
} from "@/lib/admin-api";
import { useAuthStore } from "@/stores/auth";

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
    title: "引用关系图谱渲染策略",
    entries: [
      ["兼容 SVG（原版）", "保持升级前的 Markdown 研究报告 + 原版静态 SVG 附件，适合旧客户端验证和一键回滚。"],
      ["美化 SVG（推荐）", "只发送自包含的 image/svg+xml 矢量图；支持无损缩放，不依赖 Mermaid、JavaScript、外部字体或远程资源。"],
      ["美化 SVG + Markdown", "除美化 SVG 外，再发送一份普通 Markdown 关系清单；即使客户端不预览 SVG，也能阅读论文、引用边和聚合节点明细。"],
      ["生效范围", "只影响保存后新生成的 /v1 研究地图附件。历史附件与自有 Web 前端的交互式 GenealogyGraph 均不改变。"],
      ["回滚", "重新选择“兼容 SVG（原版）”并保存即可；OpenAI 请求无需增加任何私有字段。"],
    ],
  },
  save: {
    title: "保存与版本说明",
    entries: [
      ["乐观锁", "策略带版本号。若其他管理员刚保存过，本页保存会返回 409 冲突——刷新页面拿到最新版本后再修改。"],
      ["重置修改", "放弃当前未保存的改动，回到上次保存（或服务器上）的策略。"],
      ["默认值", "首次部署默认开启工具状态行和技能行，并使用“兼容 SVG（原版）”，因此升级不会突然改变既有附件。"],
    ],
  },
};

const RENDER_STRATEGIES: Array<{
  value: ResearchMapRenderStrategy;
  label: string;
  description: string;
  recommended?: boolean;
}> = [
  {
    value: "legacy_svg",
    label: "兼容 SVG（原版）",
    description: "保留原版 SVG 与 Markdown 研究报告；默认值，适合兼容验证与回滚。",
  },
  {
    value: "pretty_svg",
    label: "美化 SVG",
    description: "自包含纯矢量图，可缩放且更接近自有前端视觉，不额外发送说明文件。",
    recommended: true,
  },
  {
    value: "pretty_svg_markdown",
    label: "美化 SVG + Markdown",
    description: "同时发送矢量图与论文/引用关系清单，适合下载、复制或 SVG 无法预览时阅读。",
  },
];

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
    if (draft.skill_card_enabled !== policy.skill_card_enabled) {
      out.skill_card_enabled = draft.skill_card_enabled;
    }
    if (draft.research_map_render_strategy !== policy.research_map_render_strategy) {
      out.research_map_render_strategy = draft.research_map_render_strategy;
    }
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

  if (!checked || loading) return <div className="flex min-h-screen items-center justify-center"><Loader2 className="h-6 w-6 animate-spin text-accent" /></div>;
  if (!user || user.role !== "administrator") return <div className="p-8 text-center text-muted">仅管理员可访问。</div>;
  if (!draft || !policy) return <div className="p-8 text-center text-error">{error || "策略加载失败"}</div>;

  return <main className="min-h-screen bg-bg px-4 py-8 text-fg sm:px-8">
    <div className="mx-auto max-w-4xl space-y-6 pb-24">
      <AdminHeader title="清小搭展示策略" icon={<LayoutGrid className="h-5 w-5" />}
        subtitle="控制 /v1 通道的状态行、技能提示与引用关系图谱附件；不影响自有前端。"
        current="/admin/display-policy" onRefresh={() => void refresh()} refreshing={loading} />

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

      <AdminSection title="引用关系图谱渲染策略" icon={<ImageIcon className="h-5 w-5 text-accent" />}
        info={<InfoButton onClick={() => setHelpItem(HELP.researchMap)} label="图谱渲染策略说明" />}>
        <div className="space-y-3">
          <p className="text-sm text-muted">
            仅控制后续 /v1 研究地图的文件附件。三种模式都使用清小搭附件协议；美化模式以
            <code className="mx-1 rounded bg-surface-hover px-1.5 py-0.5 text-xs">image/svg+xml</code>
            发送纯矢量图，不假设客户端支持 Mermaid 或可执行 HTML。
          </p>
          <div className="grid gap-3 md:grid-cols-3">
            {RENDER_STRATEGIES.map((item) => {
              const selected = draft.research_map_render_strategy === item.value;
              return (
                <button key={item.value} type="button"
                  aria-pressed={selected}
                  onClick={() => setDraft({ ...draft, research_map_render_strategy: item.value })}
                  className={`rounded-xl border p-4 text-left transition-colors ${
                    selected
                      ? "border-accent bg-accent/10 ring-1 ring-accent/30"
                      : "border-border-light bg-surface hover:bg-surface-hover"
                  }`}>
                  <div className="flex items-start justify-between gap-2">
                    <span className="text-sm font-medium text-fg">{item.label}</span>
                    {item.recommended && (
                      <span className="rounded-full bg-accent/15 px-2 py-0.5 text-[10px] font-medium text-accent">推荐</span>
                    )}
                  </div>
                  <p className="mt-2 text-xs leading-5 text-muted">{item.description}</p>
                </button>
              );
            })}
          </div>
          <p className="text-xs text-muted">
            当前选择：{RENDER_STRATEGIES.find((item) => item.value === draft.research_map_render_strategy)?.label}
            。保存后从下一轮 /v1 请求开始生效；选择“兼容 SVG（原版）”即可回滚。
          </p>
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
