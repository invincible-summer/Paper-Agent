"use client";
// Chat-only history loader: restores a saved conversation into the chat store.
// Legacy records (pre-refactor) had reserve_papers/graph_data — the backend
// already maps those onto the new session shape, so the payload here is
// uniform. Review/deep-read payloads are injected back into their tool calls
// so the cards render on reload (the tool result events carried them live).
import { useCallback } from "react";
import { useChatStore } from "@/stores/chat";
import { useUIStore } from "@/stores/ui";
import { loadChatHistory, type ChatAttachment } from "@/lib/chat-api";

interface ChatMessageDTO {
  role: "user" | "assistant";
  content: string;
  thinking?: string;
  toolCalls?: { name: string; result?: unknown }[];
  attachments?: ChatAttachment[];
}

interface ChatPayload {
  topic?: string;
  messages?: ChatMessageDTO[];
  literature_review?: string;
  paper_summaries?: Record<string, unknown>;
  map_data?: unknown;
  reading_path?: unknown[];
  attachments?: ChatAttachment[];
}

function restoreChatState(
  chat: ReturnType<typeof useChatStore.getState>,
  data: ChatPayload,
  filename: string,
) {
  chat.setCurrentChatFilename(filename);
  chat.setTopic(data.topic || "");
  chat.setSessionAttachments(data.attachments || []);
  const messages = (data.messages || []).map(m => ({
    role: m.role,
    content: m.content || "",
    thinking: m.thinking,
    toolCalls: (m.toolCalls as { name: string; result?: unknown }[] | undefined)
      ?.filter((tc) => tc.name !== "use_skill"),
    attachments: m.attachments,
  }));

  // Migration: inject top-level artifacts into their tool calls so the cards
  // (ReviewCard / DeepReadCard / ResearchMapCard / ReadingPathCard) render on
  // reload even for histories saved before the tool carried the full payload.
  const inject = (toolName: string, key: string, value: unknown) => {
    if (!value || (typeof value === "object" && Object.keys(value as object).length === 0)) return;
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.role !== "assistant" || !m.toolCalls) continue;
      const tc = m.toolCalls.find(t => t.name === toolName);
      if (!tc) continue;
      const r = (tc.result || {}) as Record<string, unknown>;
      if (!r[key]) tc.result = { ...r, [key]: value };
      return;
    }
    // No such tool call at all — append a synthetic message so the artifact
    // stays visible after reload.
    messages.push({
      role: "assistant",
      content: "",
      thinking: "",
      toolCalls: [{ name: toolName, result: { [key]: value } }],
      attachments: undefined,
    });
  };

  inject("write_review", "literature_review", data.literature_review || "");
  inject("deep_read", "summaries", data.paper_summaries);
  if (data.map_data) {
    const md = data.map_data as Record<string, unknown>;
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.role !== "assistant" || !m.toolCalls) continue;
      const tc = m.toolCalls.find(t => t.name === "research_map");
      if (!tc) continue;
      const r = (tc.result || {}) as Record<string, unknown>;
      tc.result = { clusters: [], timeline: [], landscape: "", graph: { nodes: [], edges: [] }, ...md, ...r };
      break;
    }
  }
  inject("reading_path", "path", data.reading_path && data.reading_path.length ? data.reading_path : undefined);

  chat.setMessages(messages);
}

export function useHistoryLoader() {
  return useCallback(async (filename: string) => {
    useUIStore.getState().clearActiveFile();
    try {
      const data = await loadChatHistory(filename);
      restoreChatState(useChatStore.getState(), data as ChatPayload, filename);
    } catch (e) {
      alert(e instanceof Error ? e.message : String(e));
    }
  }, []);
}
