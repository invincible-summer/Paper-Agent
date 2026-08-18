// Auth client helpers for the browser-facing multi-user channel.
// Tokens persist in localStorage and are read only after mount.

const STORAGE_KEY = "pa_auth";
const GUEST_KEY = "pa_guest_id";
const BASE = "/api/v1";

/** Persistent per-browser guest identity. The backend ignores it when local
 * mode is active and rejects it when production guest access is disabled. */
export function getGuestId(): string {
  if (typeof window === "undefined") return "";
  let gid = window.localStorage.getItem(GUEST_KEY) || "";
  if (!gid) {
    gid = (crypto.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2, 12)}`)
      .replace(/[^A-Za-z0-9-]/g, "");
    window.localStorage.setItem(GUEST_KEY, gid);
  }
  return gid;
}

export interface AuthUser {
  id: string;
  username: string;
  display_name: string;
  email: string;
  role: "local" | "guest" | "user" | "administrator";
}

export interface AuthState {
  token: string;
  user: AuthUser | null;
}

export function readAuth(): AuthState {
  if (typeof window === "undefined") return { token: "", user: null };
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return { token: "", user: null };
    const parsed = JSON.parse(raw);
    if (typeof parsed?.token === "string" && parsed.token) {
      return { token: parsed.token, user: parsed.user ?? null };
    }
  } catch { /* corrupted entry */ }
  return { token: "", user: null };
}

export function writeAuth(token: string, user: AuthUser | null): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ token, user }));
}

export function clearAuth(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(STORAGE_KEY);
}

/** Bearer token when logged in; otherwise a guest identity if available. */
export function authHeaders(): Record<string, string> {
  const { token } = readAuth();
  if (token) return { Authorization: `Bearer ${token}` };
  const gid = getGuestId();
  return gid ? { "X-Guest-Id": gid } : {};
}

export interface AuthConfig {
  auth_required: boolean;
  registration_open: boolean;
  guest_access: boolean;
}

const AUTH_CONFIG_TIMEOUT_MS = 8_000;

export async function fetchAuthConfig(): Promise<AuthConfig> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), AUTH_CONFIG_TIMEOUT_MS);
  try {
    const res = await fetch(`${BASE}/auth/config`, {
      cache: "no-store",
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(`auth config failed (${res.status})`);
    return res.json();
  } finally {
    clearTimeout(timeout);
  }
}

async function authCall(path: string, body: Record<string, string>): Promise<AuthState> {
  const res = await fetch(`${BASE}/auth/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data?.detail || `请求失败（${res.status}）`);
  return { token: data.token || "", user: data.user || null };
}

export const apiLogin = (username: string, password: string) =>
  authCall("login", { username, password });

export const apiRegister = (username: string, password: string, displayName: string) =>
  authCall("register", { username, password, display_name: displayName });

export async function apiLogout(): Promise<void> {
  try {
    await fetch(`${BASE}/auth/logout`, { method: "POST", headers: authHeaders() });
  } catch { /* best effort */ }
  clearAuth();
}

export async function apiChangePassword(
  currentPassword: string,
  newPassword: string,
): Promise<void> {
  const res = await fetch(`${BASE}/auth/change-password`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data?.detail || `修改失败（${res.status}）`);
  // The backend revokes every browser session after a password change.
  clearAuth();
}
