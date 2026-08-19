"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  BookMarked, LayoutGrid, ListChecks, Loader2, RotateCcw, Save,
} from "lucide-react";
import {
  AdminHeader, AdminSection, AdminToggle, HelpModal, InfoButton, type HelpEntry,
} from "@/components/admin/AdminUI";
import {
  AdminApiError, getDisplayPolicy, updateDisplayPolicy,
  type ApiDisplayPolicy,
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
  save: {
    title: "保存与版本说明",
    entries: [
      ["乐观锁", "策略带版本号。若其他管理员刚保存过，本页保存会返回 409 冲突——刷新页面拿到最新版本后再修改。"],
      ["重置修改", "放弃当前未保存的改动，回到上次保存（或服务器上）的策略。"],
      ["默认值", "首次部署的默认策略是工具状态行与技能行均开启，无需任何配置即有完整效果。"],
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
    if (draft.skill_card_enabled !== policy.skill_card_enabled) {
      out.skill_card_enabled = draft.skill_card_enabled;
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
      <AdminHeader title="清小搭卡片展示策略" icon={<LayoutGrid className="h-5 w-5" />}
        subtitle="控制 /v1 通道在清小搭上的工具状态行与技能提示；不影响自制前端。"
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
