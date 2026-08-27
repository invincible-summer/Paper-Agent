"use client";

/** /admin 共享布局：桌面端为左侧分组侧栏 + 右侧统一内容容器；
 *  窄屏（<lg）侧栏转为顶部横向导航条。页面的鉴权守卫和刷新动作仍由各页面自己承担。 */
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  ArrowLeft, Database, Gauge, KeyRound, LayoutGrid, Search,
  ShieldCheck, SlidersHorizontal, Trash2, Wrench, type LucideIcon,
} from "lucide-react";
import type { ReactNode } from "react";

type AdminNavItem = { href: string; label: string; icon: LucideIcon };

const NAV_GROUPS: Array<{ name: string; items: AdminNavItem[] }> = [
  {
    name: "访问与账号",
    items: [
      { href: "/admin/auth-settings", label: "访问控制", icon: ShieldCheck },
      { href: "/admin/agent-keys", label: "Agent Keys", icon: KeyRound },
      { href: "/admin/accounts-data", label: "账号数据", icon: Trash2 },
    ],
  },
  {
    name: "模型与展示",
    items: [
      { href: "/admin/display-policy", label: "卡片策略", icon: LayoutGrid },
      { href: "/admin/paper-search", label: "论文检索", icon: Search },
    ],
  },
  {
    name: "系统维护",
    items: [
      { href: "/admin/performance", label: "性能策略", icon: Gauge },
      { href: "/admin/api-storage", label: "API 存储", icon: Database },
    ],
  },
];

const ALL_ITEMS = NAV_GROUPS.flatMap((group) => group.items);

function navLinkClass(active: boolean): string {
  return active
    ? "bg-accent-soft/70 font-medium text-accent"
    : "text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg";
}

function ArrowBack() {
  return <ArrowLeft className="h-4 w-4" />;
}

export default function AdminLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="flex h-screen overflow-hidden bg-bg">
      {/* 桌面侧栏 */}
      <aside className="hidden w-56 shrink-0 flex-col border-r border-border-light bg-surface lg:flex">
        <div className="flex items-center gap-2 px-4 py-4">
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-gradient-to-br from-accent to-accent-hover shadow-sm">
            <SlidersHorizontal className="h-3.5 w-3.5 text-white" />
          </div>
          <span className="text-sm font-bold tracking-tight">管理后台</span>
        </div>
        <nav aria-label="管理后台导航" className="flex-1 overflow-y-auto px-3 pb-4">
          {NAV_GROUPS.map((group, gi) => (
            <div key={group.name} className={gi === 0 ? "" : "mt-5"}>
              <p className="mb-1.5 flex items-center gap-1.5 px-2 text-[11px] font-semibold uppercase tracking-wider text-muted">
                <Wrench className="h-3 w-3" />
                {group.name}
              </p>
              <ul className="space-y-0.5">
                {group.items.map(({ href, label, icon: Icon }) => {
                  const active = pathname === href;
                  return (
                    <li key={href}>
                      <Link href={href} aria-current={active ? "page" : undefined}
                        className={`flex h-9 items-center gap-2.5 rounded-lg px-2.5 text-[13px] ${navLinkClass(active)}`}>
                        <Icon className={`h-4 w-4 shrink-0 ${active ? "text-accent" : "text-muted"}`} />
                        {label}
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </nav>
        <div className="border-t border-border-light p-3">
          <Link href="/chat"
            className="flex h-9 items-center gap-2.5 rounded-lg px-2.5 text-[13px] text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg">
            <ArrowBack />
            返回对话
          </Link>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {/* 移动端顶部横向导航 */}
        <div className="border-b border-border-light bg-surface lg:hidden">
          <div className="flex items-center gap-2 px-4 pt-3">
            <Link href="/chat" className="flex items-center gap-1.5 text-[13px] font-semibold text-fg-secondary hover:text-fg">
              <ArrowBack />
              返回对话
            </Link>
            <span className="ml-1 text-sm font-bold tracking-tight">管理后台</span>
          </div>
          <nav aria-label="管理后台导航"
            className="flex gap-1.5 overflow-x-auto px-4 py-2.5 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
            {ALL_ITEMS.map(({ href, label, icon: Icon }) => {
              const active = pathname === href;
              return (
                <Link key={href} href={href} aria-current={active ? "page" : undefined}
                  className={`flex h-8 shrink-0 items-center gap-1.5 rounded-full px-3 text-xs ${navLinkClass(active)}`}>
                  <Icon className="h-3.5 w-3.5 shrink-0" />
                  {label}
                </Link>
              );
            })}
          </nav>
        </div>

        {/* 统一内容容器：宽度、留白由布局收敛，页面只渲染内容片段 */}
        <div className="flex-1 overflow-y-auto">
          <main className="mx-auto w-full max-w-4xl px-4 py-8 text-fg sm:px-8">{children}</main>
        </div>
      </div>
    </div>
  );
}
