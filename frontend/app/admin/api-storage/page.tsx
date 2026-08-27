"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  AlertTriangle, Check, Database, Loader2, RotateCcw, Save, ShieldCheck, Trash2,
} from "lucide-react";
import {
  AdminApiError, executeApiStorageCleanup, executeApiStorageLegacyScan,
  getApiStorageCleanupRuns, getApiStoragePolicy, getApiStorageStatus,
  getApiStorageUsage, previewApiStorageAction, updateApiStoragePolicy,
  type ApiStoragePolicy, type StorageHelpItem,
} from "@/lib/admin-api";
import {
  AdminHeader, AdminSection, ConfirmModal, HelpModal, InfoButton, type HelpEntry,
} from "@/components/admin/AdminUI";
import { useAuthStore } from "@/stores/auth";

const PRESETS: Record<string, Partial<ApiStoragePolicy>> = {
  privacy: { preset: "privacy", session_ttl_seconds: 7200, upload_ttl_seconds: 7200, max_upload_bytes: 209715200, export_ttl_seconds: 7200, cache_ttl_seconds: 2592000, trace_mode: "off" },
  balanced: { preset: "balanced", session_ttl_seconds: 604800, upload_ttl_seconds: 604800, max_upload_bytes: 209715200, export_ttl_seconds: 86400, cache_ttl_seconds: 7776000, trace_mode: "off" },
  performance: { preset: "performance", session_ttl_seconds: 2592000, upload_ttl_seconds: 2592000, max_upload_bytes: 209715200, export_ttl_seconds: 604800, cache_ttl_seconds: 15552000, trace_mode: "metadata", trace_ttl_seconds: 604800 },
};

const PRESET_NAMES: Record<string, string> = {
  privacy: "隐私优先", balanced: "均衡", performance: "性能优先",
};

const TTL_FIELDS = [
  ["session_ttl_seconds", "会话 Checkpoint"],
  ["upload_ttl_seconds", "私有上传"],
  ["export_ttl_seconds", "导出"],
  ["cache_ttl_seconds", "语义/视觉缓存"],
] as const;

const THRESHOLD_FIELDS = [
  ["observe_threshold_percent", "观察"],
  ["pressure_threshold_percent", "持续清理"],
  ["critical_threshold_percent", "关键"],
  ["hard_stop_threshold_percent", "强制保护"],
] as const;

/** StorageHelpItem 的固定字段 → 共享 HelpModal 的条目列表。 */
const STORAGE_HELP_FIELDS: Array<[string, keyof StorageHelpItem]> = [
  ["功能", "does"], ["涉及数据", "affected"], ["优点", "benefits"], ["缺点", "drawbacks"],
  ["隐私", "privacy"], ["磁盘", "disk"], ["延迟与模型费用", "latency_cost"],
  ["会话连续性", "continuity"], ["生效时间", "effective"], ["安全兜底", "fallback"],
  ["恢复默认", "restore"],
];

function storageHelpEntry(item: StorageHelpItem): HelpEntry {
  return { title: item.title, entries: STORAGE_HELP_FIELDS.map(([label, key]) => [label, item[key]]) };
}

function bytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  return `${(value / 1024 ** 3).toFixed(2)} GiB`;
}

const inputClass = "h-9 rounded-lg border border-border-light bg-bg px-2.5 text-sm text-fg outline-none transition-colors focus:border-accent/50";

interface ConfirmState {
  title: string;
  summary: string;
  execute: () => Promise<void>;
}

export default function ApiStorageAdminPage() {
  const router = useRouter();
  const checked = useAuthStore((s) => s.checked);
  const user = useAuthStore((s) => s.user);
  const signOut = useAuthStore((s) => s.signOut);
  const [policy, setPolicy] = useState<ApiStoragePolicy | null>(null);
  const [draft, setDraft] = useState<ApiStoragePolicy | null>(null);
  const [help, setHelp] = useState<Record<string, StorageHelpItem>>({});
  const [usage, setUsage] = useState<{ categories: Array<{ category: string; status: string; count: number; bytes: number }>; total_bytes: number } | null>(null);
  const [status, setStatus] = useState<{ heavy_writes_paused: boolean; disk_percent: number | null; pause_reason: string | null } | null>(null);
  const [runs, setRuns] = useState<Array<Record<string, string | number | null>>>([]);
  const [helpItem, setHelpItem] = useState<HelpEntry | null>(null);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => { void useAuthStore.getState().hydrate(); }, []);
  const handleError = useCallback((err: unknown) => {
    if (err instanceof AdminApiError && err.status === 401) { signOut(); router.replace("/login"); return; }
    setError(err instanceof Error ? err.message : "请求失败");
  }, [router, signOut]);

  const refresh = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const [p, u, s, r] = await Promise.all([getApiStoragePolicy(), getApiStorageUsage(), getApiStorageStatus(), getApiStorageCleanupRuns()]);
      setPolicy(p.policy); setDraft(p.policy); setHelp(p.help.items); setUsage(u); setStatus(s); setRuns(r.items);
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
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(draft) as Array<keyof ApiStoragePolicy>) {
      if (["version", "updated_by", "updated_at"].includes(key)) continue;
      if (draft[key] !== policy[key]) out[key] = draft[key];
    }
    return out;
  }, [policy, draft]);

  const dirty = Object.keys(changes).length > 0;

  const save = async () => {
    if (!policy || !dirty) return;
    setWorking(true); setError("");
    try {
      const result = await updateApiStoragePolicy(policy.version, changes);
      setPolicy(result.policy); setDraft(result.policy);
    } catch (err) {
      if (err instanceof AdminApiError && err.status === 409) {
        try {
          const preview = await previewApiStorageAction("policy_update", changes);
          setConfirm({
            title: "确认危险策略修改",
            summary: `该修改可能缩短现有数据保留期、启用 Full Trace 或允许紧急删除。确认令牌 10 分钟内有效且只能使用一次。预估可处理 ${String(preview.preview.deleted_artifacts ?? 0)} 个过期文件。`,
            execute: async () => {
              const result = await updateApiStoragePolicy(policy.version, changes, preview.token);
              setPolicy(result.policy); setDraft(result.policy);
            },
          });
        } catch (previewError) { handleError(previewError); }
      } else handleError(err);
    } finally { setWorking(false); }
  };

  const requestAction = async (action: "immediate_cleanup" | "legacy_scan") => {
    setWorking(true); setError("");
    try {
      const preview = await previewApiStorageAction(action);
      setConfirm({
        title: action === "legacy_scan" ? "确认扫描 API 遗留文件" : "确认立即清理 API 数据",
        summary: `仅处理 data/openai_api；不会读取或删除自制前端历史和文件。预计处理 ${String(preview.preview.deleted_artifacts ?? 0)} 个过期 artifact，约 ${bytes(Number(preview.preview.reclaimed_bytes ?? 0))}。操作不可撤销。`,
        execute: async () => {
          if (action === "legacy_scan") await executeApiStorageLegacyScan(preview.token);
          else await executeApiStorageCleanup(preview.token);
          await refresh();
        },
      });
    } catch (err) { handleError(err); } finally { setWorking(false); }
  };

  if (!checked || loading) return <div className="flex min-h-[60vh] items-center justify-center"><Loader2 className="h-6 w-6 animate-spin text-accent" /></div>;
  if (!user || user.role !== "administrator") return <div className="py-16 text-center text-muted">仅管理员可访问。</div>;
  if (!draft || !policy) return <div className="py-16 text-center text-error">{error || "策略加载失败"}</div>;

  const show = (key: string) => {
    const item = help[key];
    if (item) setHelpItem(storageHelpEntry(item));
  };

  return <>
    <div className="space-y-6 pb-24">
      <AdminHeader title="OpenAI API 存储管理" icon={<Database className="h-5 w-5" />}
        subtitle="仅管理清小搭 /v1 数据，不影响自制前端持久化。"
        onRefresh={() => void refresh()} refreshing={loading} />

      {error && <div className="rounded-lg border border-error/40 bg-error/10 p-3 text-sm text-error">{error}</div>}

      <section className="grid gap-4 md:grid-cols-3">
        <div className="rounded-xl border border-border-light bg-surface p-4">
          <Database className="mb-2 h-5 w-5 text-accent" />
          <div className="tnum text-2xl font-bold">{bytes(usage?.total_bytes ?? 0)}</div>
          <div className="text-sm text-muted">API artifact 逻辑容量</div>
        </div>
        <div className="rounded-xl border border-border-light bg-surface p-4">
          <ShieldCheck className="mb-2 h-5 w-5 text-accent" />
          <div className="tnum text-2xl font-bold">{status?.disk_percent?.toFixed(1) ?? "--"}%</div>
          <div className="text-sm text-muted">服务器磁盘占用</div>
        </div>
        <div className={`rounded-xl border p-4 ${status?.heavy_writes_paused ? "border-warning bg-warning/10" : "border-border-light bg-surface"}`}>
          <AlertTriangle className="mb-2 h-5 w-5 text-warning" />
          <div className="text-lg font-bold">{status?.heavy_writes_paused ? "文件重任务已暂停" : "文件重任务正常"}</div>
          <div className="text-sm text-muted">普通文字问答始终可用</div>
        </div>
      </section>

      <AdminSection title="策略预设"
        info={<InfoButton onClick={() => show(`preset.${draft.preset === "custom" ? "balanced" : draft.preset}`)} label="当前预设说明" />}>
        <div className="grid gap-3 sm:grid-cols-3">
          {(["privacy", "balanced", "performance"] as const).map((name) => {
            const active = draft.preset === name;
            return (
              <div key={name} role="button" tabIndex={0} aria-pressed={active}
                onClick={() => setDraft({ ...draft, ...PRESETS[name] })}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setDraft({ ...draft, ...PRESETS[name] }); }
                }}
                className={`flex h-11 cursor-pointer items-center justify-between gap-2 rounded-lg border px-4 text-left text-sm transition-colors ${
                  active ? "border-accent bg-accent/10 font-medium text-accent" : "border-border-light hover:bg-surface-hover"}`}>
                <span>{PRESET_NAMES[name]}</span>
                <span className="flex items-center gap-1">
                  {active && <Check className="h-4 w-4 text-accent" aria-label="已选中" />}
                  <InfoButton onClick={() => show(`preset.${name}`)} label={`${PRESET_NAMES[name]}说明`} />
                </span>
              </div>
            );
          })}
        </div>
      </AdminSection>

      <AdminSection title="API 文件输入"
        info={<InfoButton onClick={() => show("max_upload_bytes")} label="API 文件上限说明" />}>
        <div className="flex flex-wrap items-center justify-between gap-3 text-sm">
          <div>
            <div className="font-medium">API 远程文件上限</div>
            <div className="mt-1 max-w-2xl text-xs text-muted">
              仅影响清小搭 /v1 的 file.url 下载和 API 私有附件保存；默认 200 MiB，最多 200 MiB。
              不影响 Web /chat/upload 的 20 MiB 限制。DOC/XLS/XLSX 当前只保存、不解析。
            </div>
          </div>
          <label className="flex items-center gap-1.5">
            <input type="number" min={1} max={200} step={1}
              value={Math.round(draft.max_upload_bytes / (1024 * 1024))}
              onChange={(e) => setDraft({ ...draft, preset: "custom", max_upload_bytes: Number(e.target.value) * 1024 * 1024 })}
              className={`${inputClass} tnum w-24 text-right`} />
            <span className="w-10 text-xs text-muted">MiB</span>
          </label>
        </div>
      </AdminSection>

      <section className="grid gap-4 lg:grid-cols-2">
        <AdminSection title="关键保留期"
          info={<InfoButton onClick={() => show("ttl")} label="保留期说明" />}>
          {TTL_FIELDS.map(([key, label]) => (
            <label key={key} className="mb-3 flex items-center justify-between gap-3 text-sm">
              <span>{label}</span>
              <span className="flex items-center gap-1.5">
                <input type="number" min={1} value={Math.round(draft[key] / 3600)}
                  onChange={(e) => setDraft({ ...draft, preset: "custom", [key]: Number(e.target.value) * 3600 })}
                  className={`${inputClass} tnum w-24 text-right`} />
                <span className="w-7 text-xs text-muted">小时</span>
              </span>
            </label>
          ))}
        </AdminSection>

        <AdminSection title="磁盘阈值"
          info={<InfoButton onClick={() => show("thresholds")} label="磁盘阈值说明" />}>
          {THRESHOLD_FIELDS.map(([key, label]) => (
            <label key={key} className="mb-3 flex items-center justify-between text-sm">
              <span>{label}</span>
              <input type="number" disabled={key === "hard_stop_threshold_percent"} value={draft[key]}
                onChange={(e) => setDraft({ ...draft, preset: "custom", [key]: Number(e.target.value) })}
                className={`${inputClass} tnum w-24 text-right disabled:opacity-60`} />
            </label>
          ))}
        </AdminSection>

        <AdminSection title="95% 策略"
          info={<InfoButton onClick={() => show(`critical.${draft.critical_strategy}`)} label="95% 策略说明" />}>
          <select value={draft.critical_strategy}
            onChange={(e) => setDraft({ ...draft, preset: "custom", critical_strategy: e.target.value as ApiStoragePolicy["critical_strategy"] })}
            className={`${inputClass} w-full`}>
            <option value="pause_heavy">暂停文件重任务（推荐）</option>
            <option value="emergency_evict">紧急清理，包括最旧非活跃私有上传</option>
          </select>
        </AdminSection>

        <AdminSection title="API Trace"
          info={<InfoButton onClick={() => show(`trace.${draft.trace_mode}`)} label="Trace 说明" />}>
          <select value={draft.trace_mode}
            onChange={(e) => setDraft({ ...draft, preset: "custom", trace_mode: e.target.value as ApiStoragePolicy["trace_mode"] })}
            className={`${inputClass} w-full`}>
            <option value="off">关闭（推荐）</option>
            <option value="metadata">Metadata，最长 7 天</option>
            <option value="full">Full 脱敏排障，最长 7 天</option>
          </select>
        </AdminSection>
      </section>

      <AdminSection title="清理操作" icon={<Trash2 className="h-5 w-5 text-accent" />}
        info={<InfoButton onClick={() => show("cleanup")} label="清理操作说明" />}>
        <div className="flex flex-wrap gap-3">
          <button disabled={working} onClick={() => void requestAction("immediate_cleanup")}
            className="flex h-9 items-center gap-2 rounded-lg border border-warning px-4 text-sm text-warning transition-colors hover:bg-warning/10 disabled:opacity-50">
            <Trash2 className="h-4 w-4" />预览并立即清理
          </button>
          <button disabled={working} onClick={() => void requestAction("legacy_scan")}
            className="h-9 rounded-lg border border-border-light px-4 text-sm text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg disabled:opacity-50">
            扫描 API 遗留文件
          </button>
        </div>
      </AdminSection>

      <AdminSection title="分类容量">
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {usage?.categories.map((item) => (
            <div key={`${item.category}-${item.status}`} className="rounded-lg border border-border-light p-3 text-sm">
              <div className="font-medium">{item.category}</div>
              <div className="tnum text-muted">{item.status} · {item.count} 个 · {bytes(item.bytes)}</div>
            </div>
          ))}
        </div>
      </AdminSection>

      <AdminSection title="最近清理记录">
        <div className="space-y-2 text-sm">
          {runs.slice(0, 8).map((run) => (
            <div key={String(run.id)} className="flex justify-between rounded border border-border-light p-2">
              <span>{String(run.mode)} · {String(run.status)}</span>
              <span className="tnum text-muted">释放 {bytes(Number(run.reclaimed_bytes ?? 0))}</span>
            </div>
          ))}
          {!runs.length && <p className="text-muted">暂无清理记录</p>}
        </div>
      </AdminSection>
    </div>

    <div className="sticky bottom-0 -mx-4 border-t border-border-light bg-bg/90 px-4 backdrop-blur sm:-mx-8 sm:px-8">
      <div className="flex flex-wrap items-center justify-between gap-3 py-3">
        <span className="text-xs text-muted">版本 v{policy.version} · 上次由 {policy.updated_by} 更新</span>
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

    {confirm && (
      <ConfirmModal title={confirm.title} body={confirm.summary} danger
        confirmLabel="再次确认并执行" busy={working}
        onConfirm={() => {
          const action = confirm.execute;
          setConfirm(null);
          setWorking(true);
          void action().then(refresh).catch(handleError).finally(() => setWorking(false));
        }}
        onClose={() => setConfirm(null)} />
    )}
    <HelpModal item={helpItem} onClose={() => setHelpItem(null)} />
  </>;
}
