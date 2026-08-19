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
  max_upload_bytes: number;
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

export interface ApiDisplayPolicy {
  tool_cards_enabled: boolean;
  skill_card_enabled: boolean;
  version: number;
  updated_by: string;
  updated_at: number;
}

export interface AuthSettingsData {
  auth_required: boolean;
  guest_access: boolean;
  registration_open: boolean;
  email_requirement: "none" | "collect" | "verify";
  version: number;
  updated_by: string;
  updated_at: number;
}

export interface AuthSettingsResponse {
  settings: AuthSettingsData;
  smtp: { configured: boolean; sender: string; from_name: string };
}

export async function getAuthSettings(): Promise<AuthSettingsResponse> {
  const res = await fetch(`${BASE}/auth-settings`, { headers: authHeaders(), cache: "no-store" });
  return parseResponse(res);
}

export async function updateAuthSettings(
  expectedVersion: number,
  changes: Partial<Pick<AuthSettingsData, "auth_required" | "guest_access" | "registration_open" | "email_requirement">>,
  confirmDisableAuth = false,
): Promise<{ settings: AuthSettingsData }> {
  const res = await fetch(`${BASE}/auth-settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({
      expected_version: expectedVersion,
      confirm_disable_auth: confirmDisableAuth,
      ...changes,
    }),
  });
  return parseResponse(res);
}

export async function sendAuthTestEmail(to: string): Promise<void> {
  const res = await fetch(`${BASE}/auth-settings/test-email`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ to }),
  });
  await parseResponse(res);
}

export async function getDisplayPolicy(): Promise<{
  policy: ApiDisplayPolicy;
}> {
  const res = await fetch(`${BASE}/display-policy`, { headers: authHeaders(), cache: "no-store" });
  return parseResponse(res);
}

export async function updateDisplayPolicy(
  expectedVersion: number,
  changes: Partial<Pick<ApiDisplayPolicy, "tool_cards_enabled" | "skill_card_enabled">>,
): Promise<{ policy: ApiDisplayPolicy }> {
  const res = await fetch(`${BASE}/display-policy`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ expected_version: expectedVersion, ...changes }),
  });
  return parseResponse(res);
}

export type PaperFetchMode = "enabled" | "explicit_only" | "probe_only" | "disabled";
export type FetchPolicyDisclosure = "affected_only" | "silent";
export type PaperRoutingMode = "smart" | "all_enabled";

export interface PaperSearchPolicy {
  sources: Record<string, boolean>;
  search_deadline_seconds: number;
  per_source_timeout_seconds: number;
  verify_fulltext: boolean;
  fulltext_verify_timeout_seconds: number;
  paper_fetch_mode: PaperFetchMode;
  fetch_policy_disclosure: FetchPolicyDisclosure;
  routing_mode: PaperRoutingMode;
  version: number;
  updated_by: string;
  updated_at: number;
}

export interface PaperSourceCatalogItem {
  id: string;
  display_name: string;
  coverage: string;
  protocol: string;
  license_status: string;
  routing_tags: string[];
  requires_key: boolean;
  key_configured: boolean;
  configuration_status: string;
  operational_status: string;
  operational_reason: string;
  requires_license_confirmation: boolean;
  license_confirmation_status: "confirmed" | "not_confirmed" | "not_required";
  supports_remote_search: boolean;
  supports_search: boolean;
  supports_connectivity: boolean;
  supports_pdf_probe: boolean;
  supports_download_test: boolean;
  local_index_status: Record<string, string | number | null> | null;
}

export interface PaperSourceRuntimeStatus {
  source: string;
  state: "closed" | "open" | "half_open";
  consecutive_failures: number;
  open_until: number;
  remaining_seconds: number;
  last_http_status: number | null;
  last_latency_ms: number | null;
  last_error_code: string | null;
  last_checked_at: number | null;
}

export interface PaperSearchPolicyResponse {
  policy: PaperSearchPolicy;
  defaults: Omit<PaperSearchPolicy, "version" | "updated_by" | "updated_at">;
  quick_preset: Partial<PaperSearchPolicy>;
  source_catalog: PaperSourceCatalogItem[];
  runtime_status: PaperSourceRuntimeStatus[];
  breaker: { threshold: number; cooldown_seconds: number };
}

export interface PaperDiagnosticRun {
  id: string;
  kind: "connectivity" | "download";
  started_at: number;
  finished_at: number;
  summary: { count: number; statuses: Record<string, number> };
  items: Array<Record<string, string | number | boolean | null>>;
}

export async function getPaperSearchPolicy(): Promise<PaperSearchPolicyResponse> {
  const res = await fetch(`${BASE}/paper-search/policy`, { headers: authHeaders(), cache: "no-store" });
  return parseResponse(res);
}

export async function updatePaperSearchPolicy(
  expectedVersion: number,
  changes: Partial<Pick<PaperSearchPolicy,
    "sources" | "search_deadline_seconds" | "per_source_timeout_seconds" |
    "verify_fulltext" | "fulltext_verify_timeout_seconds" | "paper_fetch_mode" |
    "fetch_policy_disclosure" | "routing_mode">>,
): Promise<{ policy: PaperSearchPolicy }> {
  const res = await fetch(`${BASE}/paper-search/policy`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ expected_version: expectedVersion, ...changes }),
  });
  return parseResponse(res);
}

export async function runPaperConnectivity(sources?: string[]): Promise<PaperDiagnosticRun> {
  const res = await fetch(`${BASE}/paper-search/diagnostics/connectivity`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ sources: sources?.length ? sources : null }),
  });
  return parseResponse(res);
}

export async function runPaperDownloadTest(sources?: string[]): Promise<PaperDiagnosticRun> {
  const res = await fetch(`${BASE}/paper-search/diagnostics/download`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ sources: sources?.length ? sources : null }),
  });
  return parseResponse(res);
}

export async function getLatestPaperDiagnostics(): Promise<{
  connectivity: PaperDiagnosticRun | null;
  download: PaperDiagnosticRun | null;
  recent: Array<Omit<PaperDiagnosticRun, "items">>;
}> {
  const res = await fetch(`${BASE}/paper-search/diagnostics/latest`, {
    headers: authHeaders(), cache: "no-store",
  });
  return parseResponse(res);
}

export type StartupPrewarmMode = "blocking" | "background" | "role_first" | "off";
export type MapCitationMode = "fast" | "quality" | "off";
export interface PerformancePolicy {
  startup_prewarm_mode: StartupPrewarmMode;
  map_citation_mode: MapCitationMode;
  version: number;
  updated_by: string;
  updated_at: number;
}
export interface PerformancePolicyResponse {
  settings: PerformancePolicy;
  defaults: { startup_prewarm_mode: StartupPrewarmMode; map_citation_mode: MapCitationMode };
  openalex_enabled: boolean;
  effective_map_citation_mode: MapCitationMode;
  map_citation_disabled_reason: string;
  prewarm: { active_mode: string; prewarm_state: string; duration_ms: number; last_error: string };
  restart_required: boolean;
}

export async function getPerformancePolicy(): Promise<PerformancePolicyResponse> {
  const res = await fetch(`${BASE}/performance-policy`, { headers: authHeaders(), cache: "no-store" });
  return parseResponse(res);
}
export async function updatePerformancePolicy(expectedVersion: number, changes: Partial<PerformancePolicy>): Promise<PerformancePolicyResponse> {
  const res = await fetch(`${BASE}/performance-policy`, {
    method: "PUT", headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ expected_version: expectedVersion, ...changes }),
  });
  return parseResponse(res);
}
