"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { PanelLeftOpen, PanelRightOpen, Plus, FileText, BookOpen } from "lucide-react";
import { Nav } from "./Nav";
import { Sidebar } from "./Sidebar";
import { RightSidebar } from "./RightSidebar";
import { useUIStore } from "@/stores/ui";
import { useChatStore } from "@/stores/chat";
import { useAuthStore } from "@/stores/auth";

// Three-column shell: left sidebar (history, collapsible), main content,
// right sidebar (file viewer + upload list, collapsible). Both sidebars
// persist open/closed to localStorage.
//
// Auth (multi-user deployments): guest mode is configurable. When production
// disables guests, unauthenticated browsers are redirected to /login; otherwise
// each browser keeps an isolated guest identity.
// The pre-hydration render is a deterministic loading screen on both
// server and client, so this never trips the hydration matcher.
export function AppShell({
  children,
  onNewSession,
  onSelectHistory,
}: {
  children: React.ReactNode;
  onNewSession: () => void;
  onSelectHistory: (filename: string) => void;
}) {
  const { leftSidebarOpen, toggleLeftSidebar, rightSidebarOpen, toggleRightSidebar } = useUIStore();
  const authChecked = useAuthStore((s) => s.checked);
  const authRequired = useAuthStore((s) => s.authRequired);
  const guestAccess = useAuthStore((s) => s.guestAccess);
  const token = useAuthStore((s) => s.token);
  const router = useRouter();

  // Apply persisted preferences AFTER mount — stores start with deterministic
  // defaults so the server HTML and the first client render always match.
  useEffect(() => {
    useUIStore.getState().hydrate();
    useChatStore.getState().hydratePrefs();
    void useAuthStore.getState().hydrate();
  }, []);

  useEffect(() => {
    if (authChecked && authRequired && !guestAccess && !token) {
      router.replace("/login");
    }
  }, [authChecked, authRequired, guestAccess, token, router]);

  if (!authChecked || (authRequired && !guestAccess && !token)) {
    return (
      <div className="flex h-screen items-center justify-center bg-bg">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-border-light border-t-accent" />
      </div>
    );
  }

  return (
    <div className="flex h-screen overflow-hidden bg-bg">
      {leftSidebarOpen ? (
        <Sidebar onNewSession={onNewSession} onSelectHistory={onSelectHistory} onCollapse={toggleLeftSidebar} />
      ) : (
        <CollapsedRail side="left" onExpand={toggleLeftSidebar} onNewSession={onNewSession} />
      )}

      <div className="flex flex-1 flex-col overflow-hidden">
        <Nav />
        {children}
      </div>

      {rightSidebarOpen ? (
        <RightSidebar />
      ) : (
        <CollapsedRail side="right" onExpand={toggleRightSidebar} />
      )}
    </div>
  );
}

function CollapsedRail({
  side,
  onExpand,
  onNewSession,
}: {
  side: "left" | "right";
  onExpand: () => void;
  onNewSession?: () => void;
}) {
  if (side === "left") {
    return (
      <aside className="flex h-full w-12 shrink-0 flex-col items-center gap-2 border-r border-border-light bg-surface/50 py-3">
        <div className="flex h-6 w-6 items-center justify-center rounded-lg bg-accent-soft/50">
          <BookOpen className="h-3.5 w-3.5 text-accent" />
        </div>
        {onNewSession && (
          <button
            onClick={onNewSession}
            className="flex h-8 w-8 items-center justify-center rounded-lg border border-border-light text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg"
            title="新对话"
          >
            <Plus className="h-4 w-4" />
          </button>
        )}
        <button
          onClick={onExpand}
          className="mt-auto flex h-8 w-8 items-center justify-center rounded-lg text-muted transition-colors hover:bg-surface-hover hover:text-fg"
          title="展开左边栏"
        >
          <PanelLeftOpen className="h-4 w-4" />
        </button>
      </aside>
    );
  }
  return (
    <aside className="flex h-full w-12 shrink-0 flex-col items-center gap-2 border-l border-border-light bg-surface/50 py-3">
      <button
        onClick={onExpand}
        className="flex h-8 w-8 items-center justify-center rounded-lg text-muted transition-colors hover:bg-surface-hover hover:text-fg"
        title="展开右边栏"
      >
        <PanelRightOpen className="h-4 w-4" />
      </button>
      <FileText className="mt-1 h-3.5 w-3.5 text-muted/40" />
    </aside>
  );
}
