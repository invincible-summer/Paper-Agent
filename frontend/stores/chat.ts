"use client";
import { create } from "zustand";
import type { ChatAttachment } from "@/lib/chat-api";

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  thinking?: string;
  toolCalls?: { name: string; result?: unknown }[];
  isStreaming?: boolean;
  // Per-message file metadata. User messages that carried an upload list the
  // attachments here so history renders file chips after reload.
  attachments?: ChatAttachment[];
}

interface ChatState {
  messages: ChatMessage[];
  isResponding: boolean;
  currentThinking: string;
  currentAnswer: string;
  currentToolCalls: { name: string; result?: unknown }[];
  activeTool: string | null;
  toolProgress: string[];
  currentStep: string | null;
  heartbeatElapsed: number;
  historyList: {
    filename: string;
    timestamp: string;
    topic: string;
    message_count: number;
    title?: string;
    paper_count?: number;
    has_review?: boolean;
    has_map?: boolean;
    trace_ids?: string[];
    source?: string;
  }[];
  currentChatFilename: string | null;
  sessionAttachments: ChatAttachment[];
  topic: string;
  // Global UI preferences (moved from the deleted structured-page store).
  uiLang: "en" | "zh";
  theme: "light" | "dark";

  hydratePrefs: () => void;
  setUiLang: (lang: "en" | "zh") => void;
  setTheme: (theme: "light" | "dark") => void;
  setTopic: (topic: string) => void;
  setMessages: (msgs: ChatMessage[]) => void;
  addMessage: (msg: ChatMessage) => void;
  updateLastMessage: (patch: Partial<ChatMessage>) => void;
  startResponding: () => void;
  stopResponding: () => void;
  appendThinking: (text: string) => void;
  appendAnswer: (text: string) => void;
  addToolCall: (name: string) => void;
  updateToolCallResult: (result: unknown) => void;
  addToolProgress: (msg: string) => void;
  setCurrentStep: (step: string | null) => void;
  setHeartbeatElapsed: (n: number) => void;
  clearStreaming: () => void;
  setHistoryList: (list: ChatState["historyList"]) => void;
  setCurrentChatFilename: (f: string | null) => void;
  setSessionAttachments: (a: ChatAttachment[]) => void;
  patchAttachments: (updates: Array<Partial<ChatAttachment> & { id: string }>) => void;
  deleteHistory: (filename: string) => void;
  renameHistory: (filename: string, title: string) => void;
  reset: () => void;
}

function readLS(key: string): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(key);
}

export const useChatStore = create<ChatState>((set) => ({
  messages: [],
  isResponding: false,
  currentThinking: "",
  currentAnswer: "",
  currentToolCalls: [],
  activeTool: null,
  toolProgress: [],
  currentStep: null,
  heartbeatElapsed: 0,
  historyList: [],
  currentChatFilename: null,
  sessionAttachments: [],
  topic: "",
  // Deterministic defaults matching the server render; real preferences are
  // applied by hydratePrefs() after mount (see stores/ui.ts note).
  uiLang: "zh",
  theme: "light",

  hydratePrefs: () => {
    const lang = readLS("paper-agent-ui-lang");
    const theme = readLS("paper-agent-theme");
    set({
      uiLang: lang === "en" ? "en" : "zh",
      theme: theme === "dark" ? "dark" : "light",
    });
    if (typeof document !== "undefined") {
      document.documentElement.classList.toggle("dark", theme === "dark");
    }
  },
  setUiLang: (lang) => {
    set({ uiLang: lang });
    if (typeof window !== "undefined") localStorage.setItem("paper-agent-ui-lang", lang);
  },
  setTheme: (theme) => {
    set({ theme });
    if (typeof window !== "undefined") {
      localStorage.setItem("paper-agent-theme", theme);
      document.documentElement.classList.toggle("dark", theme === "dark");
    }
  },
  setTopic: (topic) => set({ topic }),
  setMessages: (msgs) => set({ messages: msgs }),
  addMessage: (msg) => set((s) => ({ messages: [...s.messages, msg] })),
  updateLastMessage: (patch) =>
    set((s) => {
      const msgs = [...s.messages];
      if (msgs.length > 0) {
        msgs[msgs.length - 1] = { ...msgs[msgs.length - 1], ...patch };
      }
      return { messages: msgs };
    }),
  startResponding: () => set({
    isResponding: true,
    currentThinking: "",
    currentAnswer: "",
    currentToolCalls: [],
    activeTool: null,
    toolProgress: [],
    currentStep: null,
    heartbeatElapsed: 0,
  }),
  stopResponding: () => set({ isResponding: false }),
  appendThinking: (text) => set((s) => ({ currentThinking: s.currentThinking + text })),
  appendAnswer: (text) => set((s) => ({ currentAnswer: s.currentAnswer + text })),
  addToolCall: (name) =>
    set((s) => ({
      activeTool: name,
      currentToolCalls: [...s.currentToolCalls, { name }],
    })),
  updateToolCallResult: (result) =>
    set((s) => {
      const calls = [...s.currentToolCalls];
      if (calls.length > 0) {
        calls[calls.length - 1] = { ...calls[calls.length - 1], result };
      }
      return { currentToolCalls: calls, activeTool: null };
    }),
  addToolProgress: (msg) =>
    set((s) => ({ toolProgress: [...s.toolProgress, msg] })),
  setCurrentStep: (step) => set({ currentStep: step }),
  setHeartbeatElapsed: (n) => set({ heartbeatElapsed: n }),
  clearStreaming: () =>
    set({
      currentThinking: "",
      currentAnswer: "",
      currentToolCalls: [],
      activeTool: null,
      toolProgress: [],
    }),
  setHistoryList: (list) => set({ historyList: list }),
  setCurrentChatFilename: (f) => set({ currentChatFilename: f }),
  setSessionAttachments: (a) => set({ sessionAttachments: a }),
  patchAttachments: (updates) => set((s) => {
    const byId = new Map(updates.map((u) => [u.id, u]));
    const patch = (a: ChatAttachment): ChatAttachment => ({ ...a, ...(byId.get(a.id) || {}) });
    return {
      sessionAttachments: s.sessionAttachments.map(patch),
      messages: s.messages.map((m) => m.attachments
        ? { ...m, attachments: m.attachments.map(patch) }
        : m),
    };
  }),
  deleteHistory: (filename) =>
    set((s) => ({
      historyList: s.historyList.filter((h) => h.filename !== filename),
      currentChatFilename: s.currentChatFilename === filename ? null : s.currentChatFilename,
    })),
  renameHistory: (filename, title) =>
    set((s) => ({
      historyList: s.historyList.map((h) =>
        h.filename === filename ? { ...h, title } : h
      ),
    })),
  reset: () =>
    set({
      messages: [],
      currentThinking: "",
      currentAnswer: "",
      currentToolCalls: [],
      activeTool: null,
      toolProgress: [],
      currentStep: null,
      heartbeatElapsed: 0,
      currentChatFilename: null,
      sessionAttachments: [],
      topic: "",
      isResponding: false,
    }),
}));
