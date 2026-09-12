"use client";
import { useEffect } from "react";
import { AppRail, MobileNavigation } from "./AppRail";
import { useUIStore } from "@/stores/ui";
import { useChatStore } from "@/stores/chat";
import { useAuthStore } from "@/stores/auth";

// Shared page frame for user-facing pages outside the chat shell: the top
// navigation rail plus one scrollable content column. Chat keeps its own
// AppShell (rail + history sidebar + right file panel).
export function AppFrame({ children }: { children: React.ReactNode }) {
  // Apply persisted preferences AFTER mount — see AppShell for the rationale
  // (deterministic first render must match the server HTML).
  useEffect(() => {
    useUIStore.getState().hydrate();
    useChatStore.getState().hydratePrefs();
    void useAuthStore.getState().hydrate();
  }, []);

  return (
    <div className="studio-shell">
      <AppRail />
      <main className="page-canvas flex min-w-0 flex-1 flex-col overflow-hidden">
        <div className="studio-mobile-header"><MobileNavigation /><span className="font-serif-display">阅研</span><small>RESEARCH STUDIO</small></div>
        {children}
      </main>
    </div>
  );
}
