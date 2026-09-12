"use client";

/** Shared admin-page UI primitives: one page header, one info-circle button,
 *  one help modal, one toggle and one confirm modal so all /admin pages stay
 *  aligned. Cross-page navigation lives in app/admin/layout.tsx (sidebar). */
import { useState, type ReactNode } from "react";
import { useOverlayFocus } from "../useOverlayFocus";
import { Button } from "../WorkbenchUI";
import {
  AlertTriangle, HelpCircle, RefreshCw,
} from "lucide-react";

/** InfoButton — the question-mark circle that opens contextual help. */
export function InfoButton({ onClick, label = "查看详细说明" }: {
  onClick: () => void; label?: string;
}) {
  return (
    <button type="button" onClick={onClick} aria-label={label} title={label}
      className="rounded-full p-1.5 text-muted transition-colors hover:bg-surface-hover hover:text-accent">
      <HelpCircle className="h-4 w-4" />
    </button>
  );
}

export interface HelpEntry {
  title: string;
  entries: Array<[string, string]>;
}

/** HelpModal — click-outside-to-close dialog rendering title + label/desc rows. */
export function HelpModal({ item, onClose }: { item: HelpEntry | null; onClose: () => void }) {
  const ref = useOverlayFocus(Boolean(item), onClose);
  if (!item) return null;
  return (
    <div ref={ref} tabIndex={-1} className="fixed inset-0 z-50 flex items-center justify-center bg-fg/40 p-4"
      onClick={onClose} role="dialog" aria-modal="true" aria-label={item.title}>
      <div className="max-h-[85vh] w-full max-w-2xl overflow-auto rounded-[7px] bg-surface p-6 shadow-lg"
        onClick={(e) => e.stopPropagation()}>
        <h2 className="mb-4 text-xl font-bold">{item.title}</h2>
        <dl className="grid gap-3 text-[13px]">
          {item.entries.map(([k, v]) => (
            <div key={k}>
              <dt className="font-semibold">{k}</dt>
              <dd className="mt-1 text-muted">{v}</dd>
            </div>
          ))}
        </dl>
        <button onClick={onClose}
          className="mt-5 rounded-[7px] bg-accent px-4 py-2 text-white hover:bg-accent-hover">
          我知道了
        </button>
      </div>
    </div>
  );
}

/** AdminSection — uniform card: icon + heading + right-aligned info button slot. */
export function AdminSection({ title, icon, info, children, className = "" }: {
  title: string;
  icon?: ReactNode;
  info?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`workbench-panel p-5 ${className}`}>
      <div className="admin-section-heading flex min-h-9 items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 font-semibold">
          {icon}<span>{title}</span>
        </h2>
        {info}
      </div>
      {children}
    </section>
  );
}

/** AdminToggle — the single settings-page switch (label sits outside; the
 *  explanatory text lives in the InfoButton help modal, not under the row). */
export function AdminToggle({ checked, onChange, disabled = false, label }: {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <button type="button" role="switch" aria-checked={checked} aria-label={label}
      disabled={disabled} onClick={() => onChange(!checked)}
      className={`relative h-6 w-11 shrink-0 rounded-full transition-colors disabled:opacity-50 ${
        checked ? "bg-accent" : "border border-border-light bg-surface-hover"
      }`}>
      <span className={`absolute top-0.5 h-5 w-5 rounded-full shadow transition-all ${
        checked ? "left-[22px] bg-white" : "left-0.5 bg-muted/60"
      }`} />
    </button>
  );
}

export function AdminSettingRow({ label, description, control, className = "" }: {
  label: ReactNode;
  description?: ReactNode;
  control: ReactNode;
  className?: string;
}) {
  return (
    <div className={`admin-setting-row flex items-center justify-between gap-4 py-2 ${className}`}>
      <div className="min-w-0">
        <div className="text-[13px] font-medium text-fg-secondary">{label}</div>
        {description && <div className="mt-1 text-[12px] leading-relaxed text-muted">{description}</div>}
      </div>
      <div className="shrink-0">{control}</div>
    </div>
  );
}

export function AdminNotice({ tone = "info", children }: {
  tone?: "info" | "success" | "warning" | "error";
  children: ReactNode;
}) {
  return <div className="my-0"><div className={`status-notice status-notice-${tone}`}>{children}</div></div>;
}

export function AdminTable({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`admin-table overflow-x-auto rounded-[7px] border border-border-light bg-surface ${className}`}>{children}</div>;
}

/** ConfirmModal — click-outside-to-close confirmation dialog. When
 *  ``confirmText`` is set (high-risk actions) the confirm button stays
 *  disabled until the administrator types exactly that text. */
export function ConfirmModal({ title, body, confirmText, confirmLabel = "确认执行",
  danger = false, busy = false, onConfirm, onClose }: {
  title: string;
  body: string;
  confirmText?: string;
  confirmLabel?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const [typed, setTyped] = useState("");
  const ref = useOverlayFocus(true, () => { if (!busy) onClose(); });
  const armed = !confirmText || typed.trim() === confirmText;
  return (
    <div ref={ref} tabIndex={-1} className="fixed inset-0 z-50 flex items-center justify-center bg-fg/40 p-4"
      onClick={() => { if (!busy) onClose(); }} role="dialog" aria-modal="true" aria-label={title}>
      <div className="w-full max-w-lg rounded-[7px] bg-surface p-6 shadow-lg"
        onClick={(e) => e.stopPropagation()}>
        {danger && <AlertTriangle className="mb-3 h-7 w-7 text-warning" />}
        <h2 className="text-xl font-bold">{title}</h2>
        <p className="mt-3 whitespace-pre-line text-[13px] text-muted">{body}</p>
        {confirmText && (
          <input value={typed} onChange={(e) => setTyped(e.target.value)}
            autoFocus placeholder={`请输入「${confirmText}」以确认`}
            className="mt-4 w-full rounded-[7px] border border-border-light bg-bg px-3 py-2 text-[13px] outline-none transition-colors focus:border-accent/50" />
        )}
        <div className="mt-5 flex justify-end gap-2">
          <Button onClick={onClose} disabled={busy}>
            取消
          </Button>
          <Button onClick={onConfirm} disabled={!armed} busy={busy} tone={danger ? "danger" : "primary"}>
            {busy ? "执行中…" : confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}

/** AdminPageHeader — icon + title + optional refresh action on the right.
 *  Cross-page navigation is provided by the admin layout sidebar. */
export function AdminHeader({ title, subtitle, icon, onRefresh, refreshing }: {
  title: string;
  subtitle?: string;
  icon?: ReactNode;
  onRefresh?: () => void;
  refreshing?: boolean;
}) {
  return (
    <header className="page-header">
      <div className="flex min-w-0 items-center gap-3">
        {icon && (
          <div className="page-header-icon">
            {icon}
          </div>
        )}
        <div className="min-w-0">
          <p className="page-eyebrow">WORKSPACE ADMINISTRATION</p>
          <h1 className="page-title !text-[26px]">{title}</h1>
          {subtitle && <p className="page-description">{subtitle}</p>}
        </div>
      </div>
      {onRefresh && (
        <button onClick={onRefresh} disabled={refreshing} aria-label="刷新" title="刷新"
          className="flex h-9 w-9 items-center justify-center rounded-[7px] text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg disabled:opacity-60">
          <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
        </button>
      )}
    </header>
  );
}
