"use client";
import { useState, useEffect } from "react";
import { Settings, Globe, Key, Sun, Moon, Check } from "lucide-react";
import { useChatStore } from "@/stores/chat";
import { t } from "@/lib/i18n";

export function SettingsPopover() {
  const { uiLang, setUiLang, theme, setTheme } = useChatStore();
  const [open, setOpen] = useState(false);
  const [apiConfigured, setApiConfigured] = useState<boolean | null>(null);
  const lang = uiLang;

  // Theme class is synced by hydratePrefs() (AppShell mount) and setTheme();
  // the inline script in layout.tsx applies the persisted theme pre-hydration.

  // Health check on open (self-contained; no global store needed).
  useEffect(() => {
    if (!open) return;
    fetch("/api/v1/health")
      .then(r => r.json())
      .then(d => setApiConfigured(Boolean(d.api_configured)))
      .catch(() => setApiConfigured(false));
  }, [open]);

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm font-medium hover:bg-surface"
      >
        <Settings className="h-4 w-4" />
        {t("settings", lang)}
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-full z-50 mt-2 w-72 rounded-xl border border-border bg-bg p-4 shadow-lg">
            {/* Theme */}
            <div className="mb-4">
              <label className="mb-1 flex items-center gap-1.5 text-sm font-semibold">
                {theme === "dark" ? <Moon className="h-4 w-4" /> : <Sun className="h-4 w-4" />}
                {t("settings_theme", lang)}
              </label>
              <div className="grid grid-cols-2 gap-2">
                <button
                  onClick={() => setTheme("light")}
                  className={`flex items-center justify-center gap-1.5 rounded-lg px-3 py-2 text-sm font-medium ${
                    theme === "light" ? "bg-accent text-white" : "border border-border hover:bg-surface"
                  }`}
                >
                  <Sun className="h-3.5 w-3.5" />
                  {t("settings_theme_light", lang)}
                </button>
                <button
                  onClick={() => setTheme("dark")}
                  className={`flex items-center justify-center gap-1.5 rounded-lg px-3 py-2 text-sm font-medium ${
                    theme === "dark" ? "bg-accent text-white" : "border border-border hover:bg-surface"
                  }`}
                >
                  <Moon className="h-3.5 w-3.5" />
                  {t("settings_theme_dark", lang)}
                </button>
              </div>
            </div>

            <hr className="my-3 border-border" />

            {/* Language */}
            <div className="mb-4">
              <label className="mb-1 flex items-center gap-1.5 text-sm font-semibold">
                <Globe className="h-4 w-4" />
                {t("settings_language", lang)}
              </label>
              <div className="grid grid-cols-2 gap-2">
                <button
                  onClick={() => setUiLang("en")}
                  className={`flex items-center justify-center gap-1.5 rounded-lg px-3 py-2 text-sm font-medium ${
                    uiLang === "en" ? "bg-accent text-white" : "border border-border hover:bg-surface"
                  }`}
                >
                  {uiLang === "en" && <Check className="h-3.5 w-3.5" />}
                  English
                </button>
                <button
                  onClick={() => setUiLang("zh")}
                  className={`flex items-center justify-center gap-1.5 rounded-lg px-3 py-2 text-sm font-medium ${
                    uiLang === "zh" ? "bg-accent text-white" : "border border-border hover:bg-surface"
                  }`}
                >
                  {uiLang === "zh" && <Check className="h-3.5 w-3.5" />}
                  中文
                </button>
              </div>
            </div>

            <hr className="my-3 border-border" />

            {/* API Key */}
            <div>
              <label className="mb-1 flex items-center gap-1.5 text-sm font-semibold">
                <Key className="h-4 w-4" />
                {t("settings_api_key", lang)}
              </label>
              {apiConfigured === null ? (
                <p className="text-sm text-muted">…</p>
              ) : apiConfigured ? (
                <p className="text-sm text-success">{t("settings_api_configured", lang)}</p>
              ) : (
                <p className="text-sm text-muted">{t("settings_api_not_configured", lang)}</p>
              )}
            </div>

            <hr className="my-3 border-border" />

            {/* Data source attribution (CC-BY / CC-BY-SA obligations) */}
            <div>
              <label className="mb-1 text-sm font-semibold">
                {t("settings_data_sources", lang)}
              </label>
              <p className="text-[11px] leading-relaxed text-muted">
                {t("settings_data_sources_body", lang)}
              </p>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
