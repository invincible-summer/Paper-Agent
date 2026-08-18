"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Loader2, Mail, RotateCcw, Save, Send, ShieldCheck, Users,
} from "lucide-react";
import {
  AdminHeader, AdminSection, AdminToggle, ConfirmModal, HelpModal, InfoButton,
  type HelpEntry,
} from "@/components/admin/AdminUI";
import {
  AdminApiError, getAuthSettings, sendAuthTestEmail, updateAuthSettings,
  type AuthSettingsData,
} from "@/lib/admin-api";
import { useAuthStore } from "@/stores/auth";

type EmailRequirement = AuthSettingsData["email_requirement"];

const EMAIL_MODES: Array<{ key: EmailRequirement; name: string }> = [
  { key: "none", name: "不要求邮箱" },
  { key: "collect", name: "仅填写邮箱" },
  { key: "verify", name: "邮箱 + 验证码" },
];

const HELP: Record<string, HelpEntry> = {
  authMode: {
    title: "账号登录（认证模式）",
    entries: [
      ["开启时", "浏览器必须登录账号才能使用；未登录访客按游客策略处理。生产部署建议开启。"],
      ["关闭时（危险）", "所有访问立即按本地单用户（local）身份运行：登录、注册、游客与管理页全部失效，历史数据隔离被穿透。"],
      ["恢复方式", "误关后需在服务器执行 scripts/enable_auth_required.py（约 5 秒生效，无需重启），或直接修改 data/users.db 中的设置行。"],
      ["与环境变量的关系", ".env 的 AUTH_REQUIRED 只在首次运行时作为初始值写入；之后以本页保存的值为准。"],
    ],
  },
  guest: {
    title: "游客访问",
    entries: [
      ["开启时", "未登录浏览器自动获得隔离的游客身份（X-Guest-Id），可直接对话，数据按游客隔离保存。"],
      ["关闭时", "已在线游客立即失去访问权限（接口开始返回 401）；生产若不允许匿名使用建议关闭。"],
      ["生效范围", "仅在账号登录开启时有效；本地单用户模式下此开关不生效。"],
    ],
  },
  register: {
    title: "开放注册",
    entries: [
      ["开启时", "登录页显示注册入口，新用户可自助注册。"],
      ["关闭时", "注册入口隐藏、注册接口返回 403；已有账号登录不受影响。生产建议关闭，由管理员统一开通账号。"],
      ["与邮箱的关系", "注册是否要求邮箱 / 验证码由下方「注册邮箱要求」单独决定。"],
    ],
  },
  email: {
    title: "注册邮箱要求",
    entries: [
      ["不要求邮箱", "注册只需用户名和密码（默认）。"],
      ["仅填写邮箱", "注册必须填写邮箱地址，但不发送验证；邮箱仅作记录，便于人工联系。"],
      ["邮箱 + 验证码", "注册必须填写邮箱并输入 6 位验证码：10 分钟有效、60 秒可重发、最多输错 5 次；同一邮箱只能绑定一个账号。需要在 .env 配置 SMTP_* 后才能启用。"],
      ["管理员豁免", "管理员账号（scripts/bootstrap_administrator.py 创建）不要求邮箱，可留空，也无需验证。"],
    ],
  },
  smtp: {
    title: "SMTP 发信与测试",
    entries: [
      ["配置位置", "SMTP 凭据只保存在服务器 .env（SMTP_HOST / PORT / SECURITY / USERNAME / PASSWORD / SENDER / FROM_NAME），绝不写入数据库或日志。修改后需重启后端。"],
      ["测试发送", "向指定邮箱发一封测试邮件，验证 SMTP 配置是否可用；启用「邮箱 + 验证码」前建议先测试。"],
      ["未配置时", "无法启用「邮箱 + 验证码」，保存会被拒绝。"],
    ],
  },
  save: {
    title: "保存与版本说明",
    entries: [
      ["乐观锁", "设置带版本号。若其他管理员刚保存过，本页保存会返回 409 冲突——刷新页面拿到最新版本后再修改。"],
      ["生效时间", "保存后立即生效（读缓存最多 5 秒延迟）；已在线的游客 / 会话会被新策略立即约束。"],
      ["与环境变量的关系", ".env 中的认证种子只在首次运行写入；之后一切以本页保存的值为准。"],
    ],
  },
};

type ConfirmState =
  | { kind: "disableAuth" }
  | { kind: "disableGuest" }
  | null;

export default function AuthSettingsAdminPage() {
  const router = useRouter();
  const checked = useAuthStore((s) => s.checked);
  const user = useAuthStore((s) => s.user);
  const signOut = useAuthStore((s) => s.signOut);
  const [settings, setSettings] = useState<AuthSettingsData | null>(null);
  const [draft, setDraft] = useState<AuthSettingsData | null>(null);
  const [smtp, setSmtp] = useState<{ configured: boolean; sender: string; from_name: string }>(
    { configured: false, sender: "", from_name: "" });
  const [helpItem, setHelpItem] = useState<HelpEntry | null>(null);
  const [confirm, setConfirm] = useState<ConfirmState>(null);
  const [authDisabled, setAuthDisabled] = useState(false);
  const [testTo, setTestTo] = useState("");
  const [testing, setTesting] = useState(false);
  const [testNotice, setTestNotice] = useState("");
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => { void useAuthStore.getState().hydrate(); }, []);
  const handleError = useCallback((err: unknown) => {
    if (err instanceof AdminApiError && err.status === 401) {
      signOut(); router.replace("/login"); return;
    }
    if (err instanceof AdminApiError && err.status === 403) {
      setAuthDisabled(true);
      setError("账号登录已关闭，管理接口不可用；请在服务器执行 scripts/enable_auth_required.py 恢复。");
      return;
    }
    setError(err instanceof Error ? err.message : "请求失败");
  }, [router, signOut]);

  const refresh = useCallback(async () => {
    setLoading(true); setError(""); setSaved(false);
    try {
      const data = await getAuthSettings();
      setSettings(data.settings); setDraft(data.settings); setSmtp(data.smtp);
    } catch (err) { handleError(err); } finally { setLoading(false); }
  }, [handleError]);

  useEffect(() => {
    if (!checked) return;
    if (!user) { router.replace("/login"); return; }
    if (user.role !== "administrator") { setLoading(false); return; }
    void refresh();
  }, [checked, user, router, refresh]);

  const changes = useMemo(() => {
    if (!settings || !draft) return {};
    const out: Partial<AuthSettingsData> = {};
    if (draft.auth_required !== settings.auth_required) out.auth_required = draft.auth_required;
    if (draft.guest_access !== settings.guest_access) out.guest_access = draft.guest_access;
    if (draft.registration_open !== settings.registration_open) {
      out.registration_open = draft.registration_open;
    }
    if (draft.email_requirement !== settings.email_requirement) {
      out.email_requirement = draft.email_requirement;
    }
    return out;
  }, [settings, draft]);

  const dirty = Object.keys(changes).length > 0;

  const applySave = async (confirmDisableAuth: boolean) => {
    if (!settings || !dirty) return;
    setWorking(true); setError(""); setSaved(false);
    try {
      const result = await updateAuthSettings(settings.version, changes, confirmDisableAuth);
      setSettings(result.settings); setDraft(result.settings);
      setSaved(true);
      if (result.settings.auth_required) {
        void getAuthSettings().then((data) => setSmtp(data.smtp)).catch(() => undefined);
      } else {
        setAuthDisabled(true);
      }
    } catch (err) { handleError(err); } finally { setWorking(false); }
  };

  const save = () => {
    if (!settings || !dirty) return;
    if (settings.auth_required && changes.auth_required === false) {
      setConfirm({ kind: "disableAuth" }); return;
    }
    if (settings.guest_access && changes.guest_access === false) {
      setConfirm({ kind: "disableGuest" }); return;
    }
    void applySave(false);
  };

  const sendTest = async () => {
    if (!testTo.trim() || testing) return;
    setTesting(true); setTestNotice("");
    try {
      await sendAuthTestEmail(testTo.trim());
      setTestNotice(`测试邮件已发送至 ${testTo.trim()}，请查收（含垃圾箱）。`);
    } catch (err) {
      setTestNotice(err instanceof Error ? err.message : "发送失败");
    } finally { setTesting(false); }
  };

  if (!checked || loading) {
    return <div className="flex min-h-screen items-center justify-center"><Loader2 className="h-6 w-6 animate-spin text-accent" /></div>;
  }
  if (!user || user.role !== "administrator") {
    return <div className="p-8 text-center text-muted">仅管理员可访问。</div>;
  }
  if (!draft || !settings) {
    return <div className="p-8 text-center text-error">{error || "设置加载失败"}</div>;
  }

  const loginControlsDisabled = !draft.auth_required;
  const verifyUnavailable = !smtp.configured;

  return <main className="min-h-screen bg-bg px-4 py-8 text-fg sm:px-8">
    <div className="mx-auto max-w-4xl space-y-6 pb-24">
      <AdminHeader title="访问控制" icon={<ShieldCheck className="h-5 w-5" />}
        subtitle="运行时控制账号登录、游客访问、开放注册与邮箱要求；保存后立即生效。"
        current="/admin/auth-settings" onRefresh={() => void refresh()} refreshing={loading} />

      {error && <div className="rounded-lg border border-error/40 bg-error/10 p-3 text-sm text-error">{error}</div>}
      {saved && !dirty && !authDisabled && (
        <div className="rounded-lg border border-success/40 bg-success/10 p-3 text-sm text-success">设置已保存并生效。</div>
      )}
      {authDisabled && (
        <div className="rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm text-fg-secondary">
          账号登录当前处于关闭状态：所有访问均按本地单用户运行，管理页与账号体系不再可用。
          重新开启请在服务器执行 <code className="rounded bg-bg px-1.5 py-0.5 text-xs">scripts/enable_auth_required.py</code>。
        </div>
      )}
      {loginControlsDisabled && !authDisabled && (
        <div className="rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm text-fg-secondary">
          本地单用户模式运行中：游客、注册与邮箱开关均不生效。
        </div>
      )}

      <AdminSection title="认证模式" icon={<ShieldCheck className="h-5 w-5 text-accent" />}
        info={<InfoButton onClick={() => setHelpItem(HELP.authMode)} label="认证模式说明" />}>
        <div className="flex items-center justify-between gap-4 py-1">
          <span className="text-sm font-medium text-fg-secondary">账号登录</span>
          <AdminToggle checked={draft.auth_required} label="账号登录"
            onChange={(next) => setDraft({ ...draft, auth_required: next })} />
        </div>
      </AdminSection>

      <AdminSection title="注册与游客" icon={<Users className="h-5 w-5 text-accent" />}
        info={<InfoButton onClick={() => setHelpItem(HELP.register)} label="开放注册说明" />}>
        <div className="flex items-center justify-between gap-4 py-1">
          <span className="flex items-center gap-1.5 text-sm font-medium text-fg-secondary">
            游客访问
            <InfoButton onClick={() => setHelpItem(HELP.guest)} label="游客访问说明" />
          </span>
          <AdminToggle checked={draft.guest_access} label="游客访问" disabled={loginControlsDisabled}
            onChange={(next) => setDraft({ ...draft, guest_access: next })} />
        </div>
        <div className="mt-3 flex items-center justify-between gap-4 border-t border-border-light pt-4">
          <span className="text-sm font-medium text-fg-secondary">开放注册</span>
          <AdminToggle checked={draft.registration_open} label="开放注册" disabled={loginControlsDisabled}
            onChange={(next) => setDraft({ ...draft, registration_open: next })} />
        </div>
      </AdminSection>

      <AdminSection title="注册邮箱要求" icon={<Mail className="h-5 w-5 text-accent" />}
        info={<InfoButton onClick={() => setHelpItem(HELP.email)} label="注册邮箱要求说明" />}>
        <div className="grid gap-2 sm:grid-cols-3" role="radiogroup" aria-label="注册邮箱要求">
          {EMAIL_MODES.map(({ key, name }) => {
            const active = draft.email_requirement === key;
            return (
              <button key={key} type="button" role="radio" aria-checked={active}
                disabled={loginControlsDisabled}
                onClick={() => setDraft({ ...draft, email_requirement: key })}
                className={`h-10 rounded-lg border px-3 text-sm transition-colors disabled:opacity-50 ${
                  active ? "border-accent bg-accent/10 font-medium text-accent"
                    : "border-border-light text-fg-secondary hover:bg-surface-hover hover:text-fg"}`}>
                {name}
                {key === "verify" && verifyUnavailable
                  ? <span className="ml-1.5 text-xs text-warning">（未配置 SMTP）</span> : null}
              </button>
            );
          })}
        </div>
      </AdminSection>

      <AdminSection title="SMTP 发信" icon={<Mail className="h-5 w-5 text-accent" />}
        info={<InfoButton onClick={() => setHelpItem(HELP.smtp)} label="SMTP 配置说明" />}>
        <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
          <span className={smtp.configured ? "text-success" : "text-warning"}>
            {smtp.configured
              ? `已配置 · 发件人 ${smtp.sender}（${smtp.from_name}）`
              : "未配置：请在服务器 .env 中填写 SMTP_* 各项后重启后端"}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <input type="email" value={testTo} onChange={(e) => setTestTo(e.target.value)}
            placeholder="收件邮箱，如 you@example.com"
            className="h-9 min-w-0 flex-1 rounded-lg border border-border-light bg-bg px-3 text-sm outline-none transition-colors focus:border-accent/50" />
          <button type="button" onClick={() => void sendTest()} disabled={!smtp.configured || testing || !testTo.trim()}
            className="flex h-9 items-center gap-1.5 rounded-lg border border-border-light px-4 text-sm text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg disabled:opacity-50">
            {testing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
            发送测试邮件
          </button>
        </div>
        {testNotice && (
          <p className={`mt-2 text-xs ${testNotice.startsWith("测试邮件已发送") ? "text-success" : "text-error"}`}>
            {testNotice}
          </p>
        )}
      </AdminSection>
    </div>

    <div className="sticky bottom-0 border-t border-border-light bg-bg/90 backdrop-blur">
      <div className="mx-auto flex max-w-4xl flex-wrap items-center justify-between gap-3 px-4 py-3 sm:px-8">
        <div className="flex items-center gap-2 text-xs text-muted">
          <InfoButton onClick={() => setHelpItem(HELP.save)} label="保存与版本说明" />
          <span>版本 v{settings.version} · 上次由 {settings.updated_by} 更新 · 保存后立即生效</span>
        </div>
        <div className="flex gap-2">
          {dirty && (
            <button onClick={() => setDraft({ ...settings })}
              className="flex h-9 items-center gap-1.5 rounded-lg border border-border-light px-4 text-sm text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg">
              <RotateCcw className="h-4 w-4" />重置修改
            </button>
          )}
          <button disabled={working || !dirty} onClick={save}
            className="flex h-9 items-center gap-1.5 rounded-lg bg-accent px-5 text-sm font-medium text-white transition-colors hover:bg-accent-hover disabled:opacity-50">
            {working ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            保存设置
          </button>
        </div>
      </div>
    </div>

    {confirm?.kind === "disableAuth" && (
      <ConfirmModal title="关闭账号登录（高危）" danger confirmText="确认" confirmLabel="确认关闭"
        busy={working}
        body={"关闭后所有未登录访问立即变为本地单用户身份：\n"
          + "· 登录、注册、游客与管理页全部失效\n"
          + "· 历史会话与上传的数据隔离被穿透\n"
          + "· 恢复需在服务器执行 scripts/enable_auth_required.py\n\n"
          + "确定要关闭吗？"}
        onConfirm={() => { setConfirm(null); void applySave(true); }}
        onClose={() => setConfirm(null)} />
    )}
    {confirm?.kind === "disableGuest" && (
      <ConfirmModal title="关闭游客访问" danger confirmLabel="确认关闭"
        body={"已在线游客将立即失去访问权限（接口返回 401），其历史数据仍保留。确定要关闭吗？"}
        onConfirm={() => { setConfirm(null); void applySave(false); }}
        onClose={() => setConfirm(null)} />
    )}

    <HelpModal item={helpItem} onClose={() => setHelpItem(null)} />
  </main>;
}
