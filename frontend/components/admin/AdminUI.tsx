"use client";

/** Shared admin-page UI primitives: one header/nav, one info-circle button,
 *  one help modal and one section card so all /admin pages stay aligned. */
import type { ReactNode } from "react";
import { useRouter } from "next/navigation";
import {
  ArrowLeft, Database, HelpCircle, KeyRound, LayoutGrid, RefreshCw, Trash2,
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
  if (!item) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose} role="dialog" aria-modal="true" aria-label={item.title}>
      <div className="max-h-[85vh] w-full max-w-2xl overflow-auto rounded-xl bg-surface p-6 shadow-lg"
        onClick={(e) => e.stopPropagation()}>
        <h2 className="mb-4 text-xl font-bold">{item.title}</h2>
        <dl className="grid gap-3 text-sm">
          {item.entries.map(([k, v]) => (
            <div key={k}>
              <dt className="font-semibold">{k}</dt>
              <dd className="mt-1 text-muted">{v}</dd>
            </div>
          ))}
        </dl>
        <button onClick={onClose}
          className="mt-5 rounded-lg bg-accent px-4 py-2 text-white hover:bg-accent-hover">
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
    <section className={`rounded-2xl border border-border-light bg-surface p-5 ${className}`}>
      <div className="mb-4 flex min-h-9 items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 font-semibold">
          {icon}<span>{title}</span>
        </h2>
        {info}
      </div>
      {children}
    </section>
  );
}

const NAV_ITEMS = [
  { href: "/admin/agent-keys", label: "Agent Keys", icon: KeyRound },
  { href: "/admin/display-policy", label: "卡片策略", icon: LayoutGrid },
  { href: "/admin/api-storage", label: "API 存储", icon: Database },
  { href: "/admin/accounts-data", label: "账号数据", icon: Trash2 },
];

/** AdminHeader — back-to-chat + title on the left, cross-page nav pills + an
 *  optional refresh action on the right. Every admin page renders this so
 *  buttons share one height and one order. */
export function AdminHeader({ title, subtitle, icon, current, onRefresh, refreshing }: {
  title: string;
  subtitle?: string;
  icon?: ReactNode;
  current: string;
  onRefresh?: () => void;
  refreshing?: boolean;
}) {
  const router = useRouter();
  return (
    <header className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex min-w-0 items-center gap-3">
        <button onClick={() => router.push("/chat")} aria-label="返回对话" title="返回对话"
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border-light text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg">
          <ArrowLeft className="h-4 w-4" />
        </button>
        {icon && (
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-accent-soft/50 text-accent">
            {icon}
          </div>
        )}
        <div className="min-w-0">
          <h1 className="truncate text-xl font-bold">{title}</h1>
          {subtitle && <p className="truncate text-xs text-muted">{subtitle}</p>}
        </div>
      </div>
      <nav className="flex flex-wrap items-center gap-2" aria-label="管理页导航">
        {NAV_ITEMS.map(({ href, label, icon: Icon }) => {
          const active = href === current;
          return (
            <button key={href} onClick={() => router.push(href)} disabled={active}
              aria-current={active ? "page" : undefined} title={active ? "当前页面" : label}
              className={`flex h-9 items-center gap-1.5 rounded-lg border px-3 text-sm transition-colors ${
                active
                  ? "cursor-default border-accent/40 bg-accent/10 font-medium text-accent"
                  : "border-border-light text-fg-secondary hover:bg-surface-hover hover:text-fg"
              }`}>
              <Icon className="h-4 w-4" />{label}
            </button>
          );
        })}
        {onRefresh && (
          <button onClick={onRefresh} disabled={refreshing} aria-label="刷新" title="刷新"
            className="flex h-9 w-9 items-center justify-center rounded-lg border border-border-light text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg disabled:opacity-60">
            <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
          </button>
        )}
      </nav>
    </header>
  );
}
