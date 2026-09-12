"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { PanelLeftOpen, PanelRightOpen, Plus, BookOpen } from "lucide-react";
import { AppRail } from "./AppRail";
import { Sidebar } from "./Sidebar";
import { RightSidebar } from "./RightSidebar";
import { useUIStore } from "@/stores/ui";
import { useChatStore } from "@/stores/chat";
import { useAuthStore } from "@/stores/auth";
import { useOverlayFocus } from "./useOverlayFocus";
import { MobileNavigation } from "./AppRail";
import { Button, IconButton } from "./WorkbenchUI";

// Shell for the chat page: top-level app rail + left history sidebar + chat
// content + right file panel. Both sidebars persist open/closed to
// localStorage.
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
  const [width, setWidth] = useState(1440);
  useEffect(() => {
    const resize = () => {
      setWidth(window.innerWidth);
      useUIStore.getState().hydrate();
    };
    setWidth(window.innerWidth);
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);

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
    <div className="studio-shell">
      <AppRail />
      {leftSidebarOpen && <ShellPanel side="left" modal={width < 1024} onClose={toggleLeftSidebar}>
        <Sidebar onNewSession={onNewSession} onSelectHistory={filename => { onSelectHistory(filename); if (width < 1024) useUIStore.getState().setLeftSidebarOpen(false); }} onCollapse={toggleLeftSidebar} />
      </ShellPanel>}

      <div className="studio-main">
        <header className="studio-toolbar">
          <MobileNavigation />
          {!leftSidebarOpen && <IconButton label="展开左边栏" onClick={toggleLeftSidebar}><PanelLeftOpen size={17} /></IconButton>}
          <div className="min-w-0 flex-1"><h1 className="studio-heading">研究对话</h1><p className="studio-caption">PAPER AGENT · RESEARCH STUDIO</p></div>
          <Button aria-label="新对话" onClick={onNewSession}><Plus size={15} /><span className="max-sm:hidden">新对话</span></Button>
          <Button aria-label="打开工作台空间" aria-expanded={rightSidebarOpen} onClick={() => { useUIStore.getState().setRightPanelTab("workbench"); useUIStore.getState().setRightSidebarOpen(!rightSidebarOpen); }}><BookOpen size={15} /><span className="max-sm:hidden">资料与阅读</span><PanelRightOpen size={14} /></Button>
        </header>
        {children}
      </div>

      {rightSidebarOpen && <ShellPanel side="right" modal={width < 1280} onClose={toggleRightSidebar}>
        <RightSidebar />
      </ShellPanel>}
    </div>
  );
}

function ShellPanel({ side, modal, onClose, children }: {
  side: "left" | "right"; modal: boolean; onClose: () => void; children: React.ReactNode;
}) {
  const ref = useOverlayFocus(modal, onClose);
  return <>
    {modal && <button className="studio-scrim" aria-label="关闭面板" onClick={onClose} tabIndex={-1} />}
    <div ref={ref} tabIndex={-1} role={modal ? "dialog" : undefined} aria-modal={modal || undefined}
      aria-label={side === "left" ? "会话历史" : "当前会话资料"}
      className={`studio-panel studio-panel-${side} ${modal ? "studio-drawer" : ""}`}>{children}</div>
  </>;
}
