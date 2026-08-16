import { authHeaders } from "@/lib/auth";

const BASE = "/api/v1/admin";

export interface AgentApiKeyItem {
  id: string;
  name: string;
  key_prefix: string;
  key_suffix: string;
  created_at: number;
  last_used_at: number | null;
  revoked_at: number | null;
}

export class AdminApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function parseResponse<T>(res: Response): Promise<T> {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new AdminApiError(res.status, data?.detail || `请求失败（${res.status}）`);
  }
  return data as T;
}

export async function listAgentApiKeys(): Promise<AgentApiKeyItem[]> {
  const res = await fetch(`${BASE}/agent-keys`, {
    headers: authHeaders(),
    cache: "no-store",
  });
  const data = await parseResponse<{ items: AgentApiKeyItem[] }>(res);
  return data.items;
}

export async function createAgentApiKey(
  name: string,
): Promise<{ item: AgentApiKeyItem; key: string }> {
  const res = await fetch(`${BASE}/agent-keys`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ name }),
  });
  return parseResponse(res);
}

export async function revokeAgentApiKey(keyId: string): Promise<void> {
  const res = await fetch(`${BASE}/agent-keys/${encodeURIComponent(keyId)}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  await parseResponse(res);
}

export interface ApiStoragePolicy {
  preset: "privacy" | "balanced" | "performance" | "custom";
  session_ttl_seconds: number;
  upload_ttl_seconds: number;
  export_ttl_seconds: number;
  public_pdf_ttl_seconds: number;
  cache_ttl_seconds: number;
  trace_ttl_seconds: number;
  cleanup_interval_minutes: number;
  observe_threshold_percent: number;
  pressure_threshold_percent: number;
  critical_threshold_percent: number;
  hard_stop_threshold_percent: 98;
  pressure_strategy: "continuous_evict";
  critical_strategy: "pause_heavy" | "emergency_evict";
  trace_mode: "off" | "metadata" | "full";
  version: number;
  updated_by: string;
  updated_at: number;
}

export interface StorageHelpItem {
  title: string; does: string; affected: string; benefits: string; drawbacks: string;
  privacy: string; disk: string; latency_cost: string; continuity: string;
  effective: string; fallback: string; restore: string;
}

export async function getApiStoragePolicy(): Promise<{
  policy: ApiStoragePolicy;
  help: { version: number; items: Record<string, StorageHelpItem> };
}> {
  const res = await fetch(`${BASE}/api-storage/policy`, { headers: authHeaders(), cache: "no-store" });
  return parseResponse(res);
}

export async function updateApiStoragePolicy(
  expectedVersion: number,
  changes: Partial<ApiStoragePolicy>,
  previewToken?: string,
): Promise<{ policy: ApiStoragePolicy; dangerous: boolean }> {
  const res = await fetch(`${BASE}/api-storage/policy`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ expected_version: expectedVersion, preview_token: previewToken, ...changes }),
  });
  return parseResponse(res);
}

export async function getApiStorageUsage() {
  const res = await fetch(`${BASE}/api-storage/usage`, { headers: authHeaders(), cache: "no-store" });
  return parseResponse<{ categories: Array<{ category: string; status: string; count: number; bytes: number }>; total_bytes: number }>(res);
}

export async function getApiStorageStatus() {
  const res = await fetch(`${BASE}/api-storage/status`, { headers: authHeaders(), cache: "no-store" });
  return parseResponse<{ text_chat_allowed: boolean; heavy_writes_paused: boolean; pause_reason: string | null; disk_percent: number | null; last_cleanup_at: number | null }>(res);
}

export async function getApiStorageCleanupRuns() {
  const res = await fetch(`${BASE}/api-storage/cleanup-runs`, { headers: authHeaders(), cache: "no-store" });
  return parseResponse<{ items: Array<Record<string, string | number | null>> }>(res);
}

export async function previewApiStorageAction(action: "immediate_cleanup" | "policy_update" | "legacy_scan" | "emergency_evict", proposed: Record<string, unknown> = {}) {
  const res = await fetch(`${BASE}/api-storage/cleanup/preview`, {
    method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ action, proposed }),
  });
  return parseResponse<{ token: string; expires_at: number; preview: Record<string, unknown> }>(res);
}

export async function executeApiStorageCleanup(token: string) {
  const res = await fetch(`${BASE}/api-storage/cleanup/execute`, {
    method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ token }),
  });
  return parseResponse<{ result: Record<string, unknown> }>(res);
}

export async function executeApiStorageLegacyScan(token: string) {
  const res = await fetch(`${BASE}/api-storage/legacy-scan`, {
    method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ token }),
  });
  return parseResponse<{ result: Record<string, unknown> }>(res);
}

export interface AccountStorageItem {
  account_id: string;
  account_type: "web_user" | "api_key";
  label: string;
  role: string;
  disabled: boolean;
  history_count: number;
  history_bytes: number;
  history_files: string[];
  paper_count: number;
  attachment_count: number;
  upload_count: number;
  upload_bytes: number;
  trace_id_count: number;
  trace_count: number;
  trace_bytes: number;
  session_count: number;
  pdf_ref_count: number;
  pdf_ref_bytes: number;
  api_sessions?: number;
  api_checkpoint_bytes?: number;
  api_private_artifacts?: number;
  api_private_bytes?: number;
}

export async function listAccountStorage(): Promise<{
  items: AccountStorageItem[];
  totals: Record<string, number>;
}> {
  const res = await fetch(`${BASE}/accounts/usage`, { headers: authHeaders(), cache: "no-store" });
  return parseResponse(res);
}

export async function cleanupAccount(
  accountType: "web_user" | "api_key",
  accountId: string,
): Promise<{ deleted: boolean; bytes: number; files: number; histories?: number; sessions?: number; artifacts?: number }> {
  const res = await fetch(`${BASE}/accounts/cleanup`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ account_type: accountType, account_id: accountId }),
  });
  return parseResponse(res);
}

export async function cleanupWebPaperCache(): Promise<{ deleted: boolean; files: number; bytes: number }> {
  const res = await fetch(`${BASE}/paper-cache/cleanup`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
  });
  return parseResponse(res);
}
