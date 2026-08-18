"use client";
import { create } from "zustand";
import {
  readAuth, writeAuth, clearAuth, fetchAuthConfig,
  type AuthUser,
} from "@/lib/auth";

interface AuthStoreState {
  /** Deterministic defaults keep server and first client render identical. */
  checked: boolean;
  authRequired: boolean;
  registrationOpen: boolean;
  guestAccess: boolean;
  token: string;
  user: AuthUser | null;
  hydrate: () => Promise<void>;
  setAuth: (token: string, user: AuthUser | null) => void;
  signOut: () => void;
}

export const useAuthStore = create<AuthStoreState>((set) => ({
  checked: false,
  authRequired: false,
  registrationOpen: false,
  guestAccess: true,
  token: "",
  user: null,

  hydrate: async () => {
    const saved = readAuth();
    // A busy/unreachable backend must not leave AppShell on an infinite spinner.
    // Preserve an existing login locally; without one, keep the old local-dev
    // fallback. Server-side authorization remains authoritative for every API.
    let cfg = {
      auth_required: Boolean(saved.token),
      registration_open: false,
      guest_access: !saved.token,
    };
    try {
      cfg = await fetchAuthConfig();
    } catch { /* bounded bootstrap fallback; backend still enforces auth */ }
    set({
      checked: true,
      authRequired: cfg.auth_required,
      registrationOpen: cfg.registration_open,
      guestAccess: cfg.guest_access,
      token: cfg.auth_required ? saved.token : "",
      user: cfg.auth_required ? saved.user : null,
    });
  },

  setAuth: (token, user) => {
    writeAuth(token, user);
    set({ token, user });
  },

  signOut: () => {
    clearAuth();
    set({ token: "", user: null });
  },
}));
