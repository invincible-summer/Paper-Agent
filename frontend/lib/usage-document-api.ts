import { authHeaders } from "@/lib/auth";

const BASE = "/api/v1";

export interface UsageDocument {
  title: string;
  content: string;
  version: number;
  updated_by: string;
  updated_at: number;
}

export class UsageDocumentApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "UsageDocumentApiError";
    this.status = status;
  }
}

async function parseResponse<T>(res: Response): Promise<T> {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new UsageDocumentApiError(res.status, data?.detail || `请求失败（${res.status}）`);
  }
  return data as T;
}

export async function getUsageDocument(): Promise<UsageDocument> {
  const res = await fetch(`${BASE}/usage-document`, { cache: "no-store" });
  return parseResponse<UsageDocument>(res);
}

export async function updateUsageDocument(
  expectedVersion: number,
  content: string,
): Promise<UsageDocument> {
  const res = await fetch(`${BASE}/admin/usage-document`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ expected_version: expectedVersion, content }),
  });
  const data = await parseResponse<{ document: UsageDocument }>(res);
  return data.document;
}

export async function uploadUsageDocumentImage(file: File): Promise<{
  filename: string;
  url: string;
  markdown: string;
  media_type: string;
  bytes: number;
  sha256: string;
}> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${BASE}/admin/usage-document/assets`, {
    method: "POST",
    headers: authHeaders(),
    body: form,
  });
  return parseResponse(res);
}
