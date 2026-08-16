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
    let cfg = { auth_required: false, registration_open: false, guest_access: true };
    try {
      cfg = await fetchAuthConfig();
    } catch { /* backend unreachable — preserve local development fallback */ }
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
