import { authHeaders } from "./auth";

// File center: the current user's uploaded attachments across ALL sessions
// (GET /api/v1/chat/files) plus per-file download of the stored original.

export interface UploadedFile {
  id: string;
  filename: string;
  ext: string;
  media_type: string;
  char_count: number;
  multimodal_status: string;
  element_count: number;
  created_at: string;
  size: number | null;
}

export async function listMyFiles(kind: "attachment" | "export" = "attachment"): Promise<UploadedFile[]> {
  const response = await fetch(`/api/v1/chat/files?kind=${kind}`, { headers: authHeaders() });
  if (!response.ok) throw new Error(`无法读取文件列表（${response.status}）`);
  const data = await response.json();
  return (data.files || []) as UploadedFile[];
}

/** Authorized download URL for a stored original (images inline, documents as attachment). */
export function fileDownloadUrl(id: string): string {
  return `/api/v1/chat/file/${encodeURIComponent(id)}/raw`;
}

export function fileDownloadName(file: UploadedFile): string {
  return file.filename || `${file.id}.${file.ext || "bin"}`;
}

export function formatFileSize(size: number | null): string {
  if (size == null || size < 0) return "—";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatDateTime(iso: string): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit" });
}
