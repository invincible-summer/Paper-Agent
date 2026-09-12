"use client";

import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Loader2 } from "lucide-react";

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  tone?: "primary" | "secondary" | "danger" | "ghost";
  busy?: boolean;
};

export function Button({ tone = "secondary", busy = false, disabled, className = "", children, type = "button", ...props }: ButtonProps) {
  return <button {...props} type={type} disabled={disabled || busy} aria-busy={busy || undefined} className={`btn-${tone} ${className}`}>
    {busy && <Loader2 size={15} className="animate-spin" />}{children}
  </button>;
}

export function IconButton({ label, ...props }: Omit<ButtonProps, "tone"> & { label: string }) {
  return <Button {...props} tone="ghost" aria-label={label} title={label} />;
}

/** Shared page primitives. These intentionally use the same low-contrast
 * paper, hairline borders and compact type scale as the PDF workbench. */
export function PageHeader({
  eyebrow,
  title,
  description,
  icon,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  icon?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div className="flex min-w-0 items-start gap-3">
        {icon && <div className="page-header-icon">{icon}</div>}
        <div className="min-w-0">
          {eyebrow && <p className="page-eyebrow">{eyebrow}</p>}
          <h1 className="page-title">{title}</h1>
          {description && <p className="page-description">{description}</p>}
        </div>
      </div>
      {actions && <div className="page-header-actions">{actions}</div>}
    </header>
  );
}

export function SectionPanel({
  title,
  description,
  actions,
  children,
  className = "",
}: {
  title?: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`workbench-panel ${className}`}>
      {(title || description || actions) && (
        <div className="workbench-panel-head">
          <div className="min-w-0">
            {title && <h2 className="workbench-panel-title">{title}</h2>}
            {description && <p className="workbench-panel-description">{description}</p>}
          </div>
          {actions && <div className="shrink-0">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

export function StatusNotice({
  tone = "info",
  children,
}: { tone?: "info" | "success" | "warning" | "error"; children: ReactNode }) {
  return <div className={`status-notice status-notice-${tone}`} role={tone === "error" ? "alert" : undefined}>{children}</div>;
}

export function EmptyState({
  icon,
  title,
  description,
  action,
  loading = false,
}: {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
  loading?: boolean;
}) {
  return (
    <div className="empty-state">
      {loading ? <Loader2 className="empty-state-icon animate-spin text-accent" /> : icon && <div className="empty-state-icon">{icon}</div>}
      <p className="empty-state-title">{title}</p>
      {description && <p className="empty-state-description">{description}</p>}
      {action && <div className="mt-3">{action}</div>}
    </div>
  );
}

export function PageTabs({ children }: { children: ReactNode }) {
  return <nav className="page-tabs" aria-label="页面分区">{children}</nav>;
}

export function ActionBar({ children }: { children: ReactNode }) {
  return <div className="action-bar">{children}</div>;
}
