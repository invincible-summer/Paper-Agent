const BASE = "/api/v1";

import { authHeaders } from "./auth";

// SSE streaming bypasses the Next.js rewrite proxy (which buffers SSE
// responses, so the frontend never sees chunks). For SSE only, call the
// backend directly; non-streaming REST calls use the Next.js proxy.
// On a deployed server set NEXT_PUBLIC_BACKEND_URL to the backend's public
// origin; the fallback matches the backend's default port (8000).
const BACKEND_DIRECT =
  process.env.NEXT_PUBLIC_BACKEND_URL ||
  (typeof window !== "undefined"
   ? `${window.location.protocol}//${window.location.hostname}:8000`
   : "http://localhost:8000");

export interface ChatHistoryItem {
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
}

export const listChatHistory = () =>
  fetch(`${BASE}/chat/history`, { headers: authHeaders() }).then(r => r.json()) as Promise<{ records: ChatHistoryItem[] }>;

export const loadChatHistory = (filename: string) =>
  fetch(`${BASE}/chat/history/${filename}`, { headers: authHeaders() }).then(r => r.json());

// D-087: an attachment uploaded via the chat paperclip. The full extracted
// text lives server-side at data/uploads/<id>.txt; only metadata is sent.
export interface ChatAttachment {
  id: string;
  filename: string;
  char_count: number;
  text_preview?: string;
  error?: string;
  ext?: string;
  media_type?: string;
  multimodal_status?: string;
  element_count?: number;
  preview_url?: string;
}

const IMAGE_EXTENSIONS = new Set(["png", "jpg", "jpeg", "webp"]);

export function isImageAttachment(attachment: ChatAttachment): boolean {
  return Boolean(
    attachment.media_type?.startsWith("image/") ||
    IMAGE_EXTENSIONS.has((attachment.ext || attachment.filename.split(".").pop() || "").toLowerCase()),
  );
}

/** Human-readable, compact metadata used by attachment chips and lists. */
export function attachmentMetaLabel(attachment: ChatAttachment): string {
  const status = attachment.multimodal_status;
  if (status === "ready") {
    const count = attachment.element_count || 0;
    return count > 0 ? `已理解 · ${count} 个元素` : "已理解";
  }
  if (status === "pending") return "待按需理解";
  if (status === "degraded") return "视觉不可用 · 已降级";
  if (status === "legacy_text_only") return "旧附件 · 仅文本";
  if (status === "unsupported") return "不支持多模态理解";
  const chars = attachment.char_count || 0;
  return chars > 0 ? `${chars} 字` : (isImageAttachment(attachment) ? "待按需理解" : "暂无文本");
}

// D-087: upload one or more supported documents/images. Returns attachment metadata
// the caller passes back to chatStream as `attachments`. Uses the Next.js
// proxy (non-streaming multipart) — small JSON response, no SSE bypass needed.
export async function uploadFiles(files: File[]): Promise<ChatAttachment[]> {
  const form = new FormData();
  for (const f of files) form.append("files", f, f.name);
  const res = await fetch(`${BASE}/chat/upload`, { method: "POST", headers: authHeaders(), body: form });
  if (!res.ok) throw new Error(`Upload failed: ${res.status}`);
  const data = await res.json();
  return (data.attachments || []) as ChatAttachment[];
}

// D-091: fetch the extracted text for an uploaded chat file so the right
// sidebar can render it when a user clicks a file chip. text lives at
// data/uploads/<id>.txt on the backend (returned via GET /chat/file/{id}).
export interface ChatFileContent {
  id: string;
  text: string;
  char_count: number;
  truncated?: boolean;
}

export async function fetchFileContent(id: string): Promise<ChatFileContent> {
  const res = await fetch(`${BASE}/chat/file/${encodeURIComponent(id)}`, { headers: authHeaders() });
  if (!res.ok) throw new Error(`File fetch failed: ${res.status}`);
  return res.json() as Promise<ChatFileContent>;
}

/** Fetch protected inline assets with the same guest/auth identity as chat APIs. */
export async function fetchFileBlob(url: string): Promise<Blob> {
  const res = await fetch(url, { headers: authHeaders() });
  if (!res.ok) throw new Error(`File preview failed: ${res.status}`);
  return res.blob();
}

/** Download a browser-channel export with the current auth/guest headers. */
export async function downloadProtectedFile(url: string, filename: string): Promise<void> {
  const blob = await fetchFileBlob(url);
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(objectUrl);
}

export interface ChatSSEEvent {
  type: string;
  [key: string]: unknown;
}

export async function* chatStream(body: {
  message: string;
  history_filename?: string | null;
  topic?: string;
  conception?: string;
  language?: string;
  field_profile?: string;
  attachments?: ChatAttachment[];
  regenerate?: boolean;
}, signal?: AbortSignal): AsyncGenerator<ChatSSEEvent> {
  // Use BACKEND_DIRECT for SSE — Next.js rewrites buffer streaming responses.
  // Optional AbortSignal lets the caller stop the in-flight turn.
  const res = await fetch(`${BACKEND_DIRECT}/api/v1/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`Chat stream failed: ${res.status}`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  // currentEvent must persist across reader.read() chunks — SSE events whose
  // data: line spans multiple chunks would otherwise lose their event type.
  let currentEvent = "message";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed.startsWith("event:")) {
        currentEvent = trimmed.slice(6).trim();
      } else if (trimmed.startsWith("data:")) {
        try {
          const payload = JSON.parse(trimmed.slice(5).trim());
          payload.type = currentEvent;
          yield payload as ChatSSEEvent;
        } catch {
          // skip malformed
        }
      }
    }
  }
}

export const deleteChatHistory = (filename: string) =>
  fetch(`${BASE}/chat/history/${filename}`, { method: "DELETE", headers: authHeaders() }).then(r => r.json());

export const renameChatHistory = (filename: string, title: string) =>
  fetch(`${BASE}/chat/history/${filename}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ title }),
  }).then(r => r.json());
