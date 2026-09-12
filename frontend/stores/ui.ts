"use client";
import { create } from "zustand";
import type { ChatAttachment } from "@/lib/chat-api";

// D-091: global UI shell state — collapsible left/right sidebars + the right
// panel's active tab and the file currently shown in the viewer. Sidebar
// collapse state persists to localStorage so it sticks across reloads.

export type RightPanelTab = "viewer" | "list" | "workbench";

interface UIState {
  leftSidebarOpen: boolean;
  rightSidebarOpen: boolean;
  rightPanelTab: RightPanelTab;
  // Top-level app rail (chat / reader / files). Collapsed = icon-only rail;
  // expanded shows labels and the file-center sub-entries. Defaults collapsed.
  railOpen: boolean;
  // File currently shown in the right viewer tab. Set by openFile() when a
  // user clicks a file chip; the viewer fetches its text on change.
  activeFileId: string | null;
  activeFileMeta: ChatAttachment | null;
  // One-shot draft text injected into the chat composer (e.g. the genealogy
  // graph's 「深问这篇」button). ChatInput consumes and clears it on change.
  composerDraft: string;

  setLeftSidebarOpen: (v: boolean) => void;
  toggleLeftSidebar: () => void;
  setRightSidebarOpen: (v: boolean) => void;
  toggleRightSidebar: () => void;
  setRightPanelTab: (t: RightPanelTab) => void;
  setRailOpen: (v: boolean) => void;
  hydrate: () => void;
  openFile: (m: ChatAttachment) => void;
  clearActiveFile: () => void;
  setComposerDraft: (d: string) => void;
}

const LS_LEFT = "paper-agent-left-sidebar-open";
const LS_RIGHT = "paper-agent-right-sidebar-open";
const LS_RAIL = "paper-agent-rail-open";

function readBool(key: string, fallback: boolean): boolean {
  if (typeof window === "undefined") return fallback;
  const v = localStorage.getItem(key);
  if (v === "1") return true;
  if (v === "0") return false;
  return fallback;
}

export const useUIStore = create<UIState>((set, get) => ({
  // Deterministic defaults — MUST match the server render. Real preferences
  // are applied by hydrate() after mount (localStorage read at module init
  // causes a hydration mismatch: server always renders the fallback).
  leftSidebarOpen: true,
  rightSidebarOpen: true,
  rightPanelTab: "workbench",
  railOpen: false,
  activeFileId: null,
  activeFileMeta: null,
  composerDraft: "",

  hydrate: () => {
    const mobile = typeof window !== "undefined" && window.matchMedia("(max-width: 1023px)").matches;
    const compact = typeof window !== "undefined" && window.matchMedia("(max-width: 1279px)").matches;
    set({
      leftSidebarOpen: mobile ? false : readBool(LS_LEFT, true),
      rightSidebarOpen: compact ? false : readBool(LS_RIGHT, true),
      railOpen: mobile ? false : readBool(LS_RAIL, false),
    });
  },
  setLeftSidebarOpen: (v) => {
    const mobile = typeof window !== "undefined" && window.innerWidth < 1024;
    set({ leftSidebarOpen: v, ...(v && mobile ? { rightSidebarOpen: false, railOpen: false } : {}) });
    if (typeof window !== "undefined" && window.innerWidth >= 1024) localStorage.setItem(LS_LEFT, v ? "1" : "0");
  },
  toggleLeftSidebar: () => get().setLeftSidebarOpen(!get().leftSidebarOpen),
  setRightSidebarOpen: (v) => {
    const mobile = typeof window !== "undefined" && window.innerWidth < 1024;
    set({ rightSidebarOpen: v, ...(v && mobile ? { leftSidebarOpen: false, railOpen: false } : {}) });
    if (typeof window !== "undefined" && window.innerWidth >= 1280) localStorage.setItem(LS_RIGHT, v ? "1" : "0");
  },
  toggleRightSidebar: () => get().setRightSidebarOpen(!get().rightSidebarOpen),
  setRightPanelTab: (t) => set({ rightPanelTab: t }),
  setRailOpen: (v) => {
    const mobile = typeof window !== "undefined" && window.innerWidth < 1024;
    set({ railOpen: v, ...(v && mobile ? { leftSidebarOpen: false, rightSidebarOpen: false } : {}) });
    if (typeof window !== "undefined" && window.innerWidth >= 1024) localStorage.setItem(LS_RAIL, v ? "1" : "0");
  },
  // Click a file chip: open the right sidebar (if collapsed), switch to the
  // viewer tab, and set the file to display. The viewer component observes
  // activeFileId and fetches its content.
  openFile: (m) =>
    set({
      activeFileId: m.id,
      activeFileMeta: m,
      rightPanelTab: "viewer",
      rightSidebarOpen: true,
      ...(typeof window !== "undefined" && window.innerWidth < 1024 ? { leftSidebarOpen: false, railOpen: false } : {}),
    }),
  clearActiveFile: () => set({ activeFileId: null, activeFileMeta: null }),
  setComposerDraft: (d) => set({ composerDraft: d }),
}));
