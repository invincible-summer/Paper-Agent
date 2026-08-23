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

export interface ApiDisplayPolicy {
  tool_cards_enabled: boolean;
  skill_card_enabled: boolean;
  research_map_svg_enabled: boolean;
  research_map_mermaid_enabled: boolean;
  research_map_html_enabled: boolean;
  research_map_markdown_enabled: boolean;
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
  changes: Partial<Pick<
    ApiDisplayPolicy,
    "tool_cards_enabled" | "skill_card_enabled" | "research_map_svg_enabled"
    | "research_map_mermaid_enabled" | "research_map_html_enabled"
    | "research_map_markdown_enabled"
  >>,
): Promise<{ policy: ApiDisplayPolicy }> {
  const res = await fetch(`${BASE}/display-policy`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ expected_version: expectedVersion, ...changes }),
  });
  return parseResponse(res);
}

export type PaperRoutingMode = "smart" | "all_enabled";
export type PaperCapability = "search" | "abstract";
export type CapabilityDiagnosticStatus = "ok" | "slow" | "failed" | "not_applicable" | "not_configured" | "empty";

export interface PaperCapabilityState {
  enabled: boolean;
  disabled_by: "administrator" | "diagnostic" | null;
  reason_code: string | null;
  reason: string | null;
  disabled_at: number | null;
  last_checked_at: number | null;
  last_diagnostic_status: CapabilityDiagnosticStatus | null;
  last_latency_ms: number | null;
}

export interface PaperSearchPolicy {
  sources: Record<string, boolean>;
  capabilities: Record<string, Record<PaperCapability, PaperCapabilityState>>;
  search_deadline_seconds: number;
  per_source_timeout_seconds: number;
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
  supports_abstract: boolean;
  official_docs_url: string;
  diagnostic_method: "official_api";
  local_index_status: Record<string, string | number | null> | null;
}

export interface CapabilityDiagnostic {
  status: CapabilityDiagnosticStatus;
  latency_ms: number | null;
  result_count?: number;
  valid_abstract_count?: number;
  abstract_length?: number;
  error_code: string | null;
  message: string | null;
}

export interface PlatformDiagnostic {
  source: string;
  connectivity: CapabilityDiagnostic;
  search: CapabilityDiagnostic;
  abstract: CapabilityDiagnostic;
  auto_disabled_capabilities: PaperCapability[];
}

export interface PaperSearchPolicyResponse {
  policy: PaperSearchPolicy;
  capabilities: PaperSearchPolicy["capabilities"];
  defaults: Omit<PaperSearchPolicy, "version" | "updated_by" | "updated_at">;
  quick_preset: Partial<PaperSearchPolicy>;
  source_catalog: PaperSourceCatalogItem[];
  configuration_status: Record<string, string>;
  disabled_by: Record<string, Record<PaperCapability, PaperCapabilityState["disabled_by"]>>;
  disabled_reason: Record<string, Record<PaperCapability, string | null>>;
  diagnostic_summary: PaperDiagnosticRun | null;
  last_checked_at: number | null;
}

export interface PaperDiagnosticRun {
  id: string;
  kind: "capability";
  started_at: number;
  finished_at: number;
  summary: { count: number; statuses: Record<string, number>; auto_disabled_capabilities?: Array<{ source: string; capability: PaperCapability }> };
  items: Array<Record<string, string | number | boolean | null>>;
  platforms?: PlatformDiagnostic[];
  policy?: PaperSearchPolicy;
}

export async function getPaperSearchPolicy(): Promise<PaperSearchPolicyResponse> {
  const res = await fetch(`${BASE}/paper-search/policy`, { headers: authHeaders(), cache: "no-store" });
  return parseResponse(res);
}

export async function updatePaperSearchPolicy(
  expectedVersion: number,
  changes: Partial<Pick<PaperSearchPolicy,
    "sources" | "search_deadline_seconds" | "per_source_timeout_seconds" |
    "routing_mode">>,
): Promise<{ policy: PaperSearchPolicy }> {
  const res = await fetch(`${BASE}/paper-search/policy`, {
    method: "PUT", headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ expected_version: expectedVersion, ...changes }),
  });
  return parseResponse(res);
}

export async function setPaperSourceCapability(
  source: string, capability: PaperCapability, enabled: boolean, expectedVersion: number,
): Promise<{ policy: PaperSearchPolicy; capability: PaperCapabilityState }> {
  const res = await fetch(`${BASE}/paper-search/sources/${encodeURIComponent(source)}/capabilities/${capability}`, {
    method: "PUT", headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ expected_version: expectedVersion, enabled }),
  });
  return parseResponse(res);
}

export async function runPaperCapabilityDiagnostics(
  sources?: string[], capability?: "connectivity" | PaperCapability,
): Promise<PaperDiagnosticRun> {
  const res = await fetch(`${BASE}/paper-search/diagnostics/capabilities`, {
    method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ sources: sources?.length ? sources : null, capability: capability || null }),
  });
  return parseResponse(res);
}

export async function getLatestPaperDiagnostics(): Promise<{
  capability: PaperDiagnosticRun | null;
  recent: Array<Omit<PaperDiagnosticRun, "items">>;
}> {
  const res = await fetch(`${BASE}/paper-search/diagnostics/latest`, { headers: authHeaders(), cache: "no-store" });
  return parseResponse(res);
}

export interface ToolBudgetCatalogItem {
  name: string;
  label: string;
  category: string;
  description: string;
  recommended_max: number;
  default_seconds: number;
  current_seconds: number;
  overridden: boolean;
}

export interface ToolBudgetPolicyData {
  budgets: Record<string, number>;
  default_budget_seconds: number;
  reserve_seconds: number;
  api_turn_soft_seconds: number;
  api_turn_hard_seconds: number;
  version: number;
  updated_by: string;
  updated_at: number;
}

export interface ToolBudgetLimits {
  min_seconds: number;
  max_seconds: number;
  min_reserve: number;
  max_reserve: number;
  gateway_timeout_seconds: number;
  min_api_turn_soft_seconds: number;
  max_api_turn_soft_seconds: number;
  api_turn_soft_seconds: number;
  min_api_turn_hard_seconds: number;
  api_turn_hard_seconds: number;
  web_turn_soft_seconds: number;
  web_turn_hard_seconds: number;
}

export interface ToolBreakerState {
  tool: string;
  state: "closed" | "open" | "half_open";
  consecutive_failures: number;
  remaining_seconds: number;
}

export interface ToolBudgetsResponse {
  policy: ToolBudgetPolicyData;
  catalog: ToolBudgetCatalogItem[];
  limits: ToolBudgetLimits;
  breaker: { threshold: number; cooldown_seconds: number };
  breaker_states: Record<string, ToolBreakerState>;
}

export async function getToolBudgets(): Promise<ToolBudgetsResponse> {
  const res = await fetch(`${BASE}/tool-budgets`, { headers: authHeaders(), cache: "no-store" });
  return parseResponse(res);
}

export async function updateToolBudgets(
  expectedVersion: number,
  changes: {
    budgets?: Record<string, number>;
    default_budget_seconds?: number;
    reserve_seconds?: number;
    api_turn_soft_seconds?: number;
    api_turn_hard_seconds?: number;
  },
): Promise<ToolBudgetsResponse> {
  const res = await fetch(`${BASE}/tool-budgets`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ expected_version: expectedVersion, ...changes }),
  });
  return parseResponse(res);
}

export async function recoverToolBreaker(tool: string): Promise<{
  tool: string;
  breaker_states: Record<string, ToolBreakerState>;
}> {
  const res = await fetch(`${BASE}/tool-budgets/breaker/recover`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ tool }),
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
