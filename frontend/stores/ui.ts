"use client";
import { create } from "zustand";
import type { ChatAttachment } from "@/lib/chat-api";

// D-091: global UI shell state — collapsible left/right sidebars + the right
// panel's active tab and the file currently shown in the viewer. Sidebar
// collapse state persists to localStorage so it sticks across reloads.

export type RightPanelTab = "viewer" | "list";

interface UIState {
  leftSidebarOpen: boolean;
  rightSidebarOpen: boolean;
  rightPanelTab: RightPanelTab;
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
  hydrate: () => void;
  openFile: (m: ChatAttachment) => void;
  clearActiveFile: () => void;
  setComposerDraft: (d: string) => void;
}

const LS_LEFT = "paper-agent-left-sidebar-open";
const LS_RIGHT = "paper-agent-right-sidebar-open";

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
  rightPanelTab: "list",
  activeFileId: null,
  activeFileMeta: null,
  composerDraft: "",

  hydrate: () => {
    set({
      leftSidebarOpen: readBool(LS_LEFT, true),
      rightSidebarOpen: readBool(LS_RIGHT, true),
    });
  },
  setLeftSidebarOpen: (v) => {
    set({ leftSidebarOpen: v });
    if (typeof window !== "undefined") localStorage.setItem(LS_LEFT, v ? "1" : "0");
  },
  toggleLeftSidebar: () => get().setLeftSidebarOpen(!get().leftSidebarOpen),
  setRightSidebarOpen: (v) => {
    set({ rightSidebarOpen: v });
    if (typeof window !== "undefined") localStorage.setItem(LS_RIGHT, v ? "1" : "0");
  },
  toggleRightSidebar: () => get().setRightSidebarOpen(!get().rightSidebarOpen),
  setRightPanelTab: (t) => set({ rightPanelTab: t }),
  // Click a file chip: open the right sidebar (if collapsed), switch to the
  // viewer tab, and set the file to display. The viewer component observes
  // activeFileId and fetches its content.
  openFile: (m) =>
    set({
      activeFileId: m.id,
      activeFileMeta: m,
      rightPanelTab: "viewer",
      rightSidebarOpen: true,
    }),
  clearActiveFile: () => set({ activeFileId: null, activeFileMeta: null }),
  setComposerDraft: (d) => set({ composerDraft: d }),
}));
