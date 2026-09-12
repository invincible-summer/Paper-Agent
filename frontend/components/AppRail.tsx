"use client";
import { Suspense, useEffect, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  BookOpen, BookOpenText, FolderOpen, LogIn, LogOut, MessageSquare,
  MessageSquarePlus, PanelLeftClose, PanelLeftOpen, ShieldCheck, User, Menu,
} from "lucide-react";
import { useUIStore } from "@/stores/ui";
import { useAuthStore } from "@/stores/auth";
import { useChatStore } from "@/stores/chat";
import { apiLogout } from "@/lib/auth";
import { t } from "@/lib/i18n";
import { SettingsPopover } from "./SettingsPopover";
import { useOverlayFocus } from "./useOverlayFocus";

// Top-level navigation rail mounted on every user-facing page: chat, the
// reading-studio entry and the file center, plus secondary entries (usage
// doc / feedback / settings / account) at the bottom. Collapsed it is an
// icon-only 56px strip; expanded (200px) it shows labels and the file-center
// sub-entries. The chat history sidebar sits to its right as a second level.

type RailItem = {
  href: string;
  icon: typeof MessageSquare;
  label: string;
  match: (pathname: string, tab: string | null) => boolean;
  children?: { href: string; label: string; match: (tab: string | null) => boolean }[];
};

const ITEMS: RailItem[] = [
  { href: "/chat", icon: MessageSquare, label: "对话",
    match: (p) => p === "/chat" || p.startsWith("/chat/") },
  { href: "/reader", icon: BookOpenText, label: "阅研",
    match: (p) => p === "/reader" },
  {
    href: "/files", icon: FolderOpen, label: "文件中心",
    match: (p) => p === "/files",
    children: [
      { href: "/files?tab=papers", label: "论文集", match: (tab) => tab !== "uploads" },
      { href: "/files?tab=uploads", label: "个人文件", match: (tab) => tab === "uploads" },
    ],
  },
];

export function AppRail() {
  return (
    <Suspense fallback={null}>
      <RailInner />
    </Suspense>
  );
}

export function MobileNavigation() {
  const setRailOpen = useUIStore(s => s.setRailOpen);
  return <button className="btn-ghost studio-mobile-nav" aria-label="展开导航" onClick={() => setRailOpen(true)}><Menu size={18} /></button>;
}

function RailInner() {
  const railOpen = useUIStore((s) => s.railOpen);
  const setRailOpen = useUIStore((s) => s.setRailOpen);
  const pathname = usePathname() || "/";
  const searchParams = useSearchParams();
  const tab = searchParams.get("tab");
  const search = searchParams.toString();
  const router = useRouter();
  const { uiLang: lang } = useChatStore();
  const authRequired = useAuthStore((s) => s.authRequired);
  const user = useAuthStore((s) => s.user);
  const guestAccess = useAuthStore((s) => s.guestAccess);
  const registrationOpen = useAuthStore((s) => s.registrationOpen);
  const signOut = useAuthStore((s) => s.signOut);
  const [mobile, setMobile] = useState(false);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 1023px)");
    const change = () => setMobile(media.matches);
    change(); media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);
  const ref = useOverlayFocus(mobile && railOpen, () => setRailOpen(false));

  // A persisted expanded rail is useful on desktop, but on small screens it
  // is a drawer. Close it after route changes so the new page is never left
  // underneath the previous drawer; this runs after mount and cannot affect
  // the server render.
  useEffect(() => {
    if (typeof window !== "undefined" && window.matchMedia("(max-width: 1023px)").matches) {
      setRailOpen(false);
    }
  }, [pathname, search, setRailOpen]);

  const handleLogout = async () => {
    await apiLogout();
    signOut();
    router.replace(guestAccess ? "/chat" : "/login");
    router.refresh();
  };

  return (
    <>
    <button
      type="button"
      aria-label="关闭导航"
      onClick={() => setRailOpen(false)}
      tabIndex={-1}
      className={`studio-scrim lg:hidden ${railOpen ? "block" : "hidden"}`}
    />
    <div ref={ref} tabIndex={-1} role={mobile && railOpen ? "dialog" : undefined} aria-modal={mobile && railOpen || undefined} aria-label="主导航"
      className={`app-rail ${railOpen ? "app-rail-expanded" : ""}`}
    >
      {/* Brand */}
      <button
        onClick={() => router.push("/chat")}
        title={t("app_title", lang)}
        className="flex h-[52px] shrink-0 items-center gap-2.5 border-b border-border-light px-3"
      >
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[7px] bg-accent-soft/60">
          <BookOpen className="h-4.5 w-4.5 text-accent" />
        </span>
        {railOpen && (
          <span className="min-w-0 text-left">
            <span className="font-serif-display block truncate text-[15px] font-bold leading-tight text-fg">
              {t("app_title", lang)}
            </span>
            <span className="block text-[8px] uppercase leading-tight tracking-[0.15em] text-muted">
              Paper Agent
            </span>
          </span>
        )}
      </button>

      {/* Primary entries */}
      <nav className="flex flex-col gap-0.5 px-2 py-3">
        {ITEMS.map((item) => {
          const active = item.match(pathname, tab) || (item.children || []).some((c) =>
            pathname === "/files" && c.match(tab));
          return (
            <div key={item.href}>
              <button
                onClick={() => router.push(item.href)}
                aria-label={item.label}
                aria-current={active ? "page" : undefined}
                title={railOpen ? undefined : item.label}
                className={`flex h-9 w-full items-center gap-2.5 rounded-[7px] px-2.5 text-[13px] transition-colors ${
                  active
                    ? "bg-accent-soft font-semibold text-accent"
                    : "text-fg-secondary hover:bg-surface-hover hover:text-accent"
                }`}
              >
                <item.icon className="h-4 w-4 shrink-0" />
                {railOpen && <span className="truncate">{item.label}</span>}
              </button>
              {railOpen && item.children && active && (
                <div className="mt-0.5 flex flex-col gap-0.5 pl-6">
                  {item.children.map((child) => {
                    const childActive = pathname === "/files" && child.match(tab);
                    return (
                      <button
                        key={child.href}
                        onClick={() => router.push(child.href)}
                        className={`flex h-7 items-center rounded-[6px] px-2.5 text-[12px] transition-colors ${
                          childActive
                            ? "bg-accent-soft/70 font-medium text-accent"
                            : "text-muted hover:bg-surface-hover hover:text-accent"
                        }`}
                      >
                        {child.label}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </nav>

      <div className="flex-1" />

      {/* Secondary entries */}
      <div className="flex flex-col gap-0.5 px-2 pb-2">
        <button
          onClick={() => router.push("/usage-doc")}
          title="使用文档"
          className={`flex h-8 w-full items-center gap-2.5 rounded-[7px] px-2.5 text-[12px] transition-colors ${
            pathname === "/usage-doc"
              ? "bg-accent-soft/70 text-accent"
              : "text-muted hover:bg-surface-hover hover:text-accent"
          }`}
        >
          <BookOpenText className="h-3.5 w-3.5 shrink-0" />
          {railOpen && <span>使用文档</span>}
        </button>
        <button
          onClick={() => router.push("/feedback")}
          title="意见反馈"
          className={`flex h-8 w-full items-center gap-2.5 rounded-[7px] px-2.5 text-[12px] transition-colors ${
            pathname === "/feedback"
              ? "bg-accent-soft/70 text-accent"
              : "text-muted hover:bg-surface-hover hover:text-accent"
          }`}
        >
          <MessageSquarePlus className="h-3.5 w-3.5 shrink-0" />
          {railOpen && <span>意见反馈</span>}
        </button>
      </div>

      {/* Account */}
      <div className="flex flex-col gap-0.5 border-t border-border-light px-2 py-2">
        {authRequired && user?.role === "administrator" && (
          <button
            onClick={() => router.push("/admin/auth-settings")}
            title="管理后台"
            className="flex h-8 w-full items-center gap-2.5 rounded-[7px] px-2.5 text-[12px] text-muted transition-colors hover:bg-surface-hover hover:text-accent"
          >
            <ShieldCheck className="h-3.5 w-3.5 shrink-0" />
            {railOpen && <span>管理后台</span>}
          </button>
        )}
        {authRequired && user ? (
          <button
            onClick={handleLogout}
            title={guestAccess ? "退出登录（回到游客模式）" : "退出登录"}
            className="flex h-8 w-full items-center gap-2.5 rounded-[7px] px-2.5 text-[12px] text-muted transition-colors hover:bg-surface-hover hover:text-accent"
          >
            <LogOut className="h-3.5 w-3.5 shrink-0" />
            {railOpen && <span className="truncate">{user.display_name || user.username}</span>}
          </button>
        ) : authRequired ? (
          <button
            onClick={() => router.push("/login")}
            className="flex h-8 w-full items-center gap-2.5 rounded-[7px] px-2.5 text-[12px] text-muted transition-colors hover:bg-surface-hover hover:text-accent"
            title={registrationOpen ? "登录 / 注册" : "登录"}
          >
            <LogIn className="h-3.5 w-3.5 shrink-0" />
            {railOpen && <span>{registrationOpen ? "登录 / 注册" : "登录"}</span>}
          </button>
        ) : null}

        <div className={`flex items-center gap-1 ${railOpen ? "" : "flex-col"}`}>
          <SettingsPopover />
          <button
            onClick={() => setRailOpen(!railOpen)}
            title={railOpen ? "收起导航" : "展开导航"}
            className="btn-ghost h-8 w-8"
          >
            {railOpen ? <PanelLeftClose className="h-4 w-4" /> : <PanelLeftOpen className="h-4 w-4" />}
          </button>
        </div>
        {!railOpen && authRequired && (
          <div className="flex justify-center pt-0.5 text-muted" title={user ? (user.display_name || user.username) : (guestAccess ? "游客" : "未登录")}>
            <User className="h-3 w-3" />
          </div>
        )}
      </div>
    </div>
    </>
  );
}
