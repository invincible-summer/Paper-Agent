import { authHeaders } from "./auth";
import type { ChatAttachment, ChatSSEEvent } from "./chat-api";
import { BACKEND_DIRECT } from "./chat-api";

export interface Anchor {
  id: string; version: number; page: number; quote: string; rects: number[][];
  fingerprint: string; verified: boolean; precision: "text" | "page";
}
export interface ReaderNote {
  id: string; version: number; anchor_id: string; category: string; note: string; interpretation: string;
}
export interface ReaderThread {
  id: string; version: number; anchor_id: string; title: string;
  messages: { role: "user" | "assistant"; content: string; thinking?: string }[];
}
export interface Position {
  page: number; zoom: number; view: "continuous" | "single" | "spread";
  goal: string; glossary: string; fingerprint: string; version: number;
}
export interface ReaderDocument {
  fingerprint: string; page_count: number; outline: { level: number; title: string; page: number }[];
  attachment: ChatAttachment; documents: ChatAttachment[]; session_id: string; history_filename: string;
  title: string; topic: string; position: Position | null; anchors: Anchor[]; notes: ReaderNote[]; threads: ReaderThread[];
}
export interface Selection {
  page: number; quote: string; rects: number[][];
}
export const documentPath = (sid: string, aid: string) => `/api/v1/reader/sessions/${encodeURIComponent(sid)}/documents/${encodeURIComponent(aid)}`;
export const readerHref = (sid: string, aid: string, anchor?: string) => `/chat/${sid}/read/${aid}${anchor ? `#anchor=${anchor}` : ""}`;

export async function readerCall<T>(path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { method, headers: { ...authHeaders(), ...(body ? { "Content-Type": "application/json" } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}), signal, cache: "no-store" });
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : `请求失败（${response.status}）`);
  return value as T;
}
export const openReader = (attachmentId: string, historyFilename: string | null) =>
  readerCall<{ session_id: string; history_filename: string; attachment_id: string }>("/api/v1/reader/open", "POST",
    { attachment_id: attachmentId, history_filename: historyFilename });

export async function* readerAction(path: string, body: unknown, signal: AbortSignal): AsyncGenerator<ChatSSEEvent> {
  const response = await fetch(`${BACKEND_DIRECT}${path}/actions`, {
    method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify(body), signal,
  });
  if (!response.ok || !response.body) {
    const value = await response.json().catch(() => ({}));
    throw new Error(typeof value.detail === "string" ? value.detail : `助读请求失败（${response.status}）`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() || "";
      for (const frame of frames) {
        const data = frame.split("\n").find(line => line.startsWith("data: "));
        if (data) yield JSON.parse(data.slice(6));
      }
    }
  } finally { reader.releaseLock(); }
}
