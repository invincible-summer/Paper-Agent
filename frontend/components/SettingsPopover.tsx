"use client";
import { useState, useEffect } from "react";
import { Settings, Globe, Key, Check } from "lucide-react";
import { useChatStore } from "@/stores/chat";
import { t } from "@/lib/i18n";
import { useOverlayFocus } from "./useOverlayFocus";

export function SettingsPopover() {
  const { uiLang, setUiLang } = useChatStore();
  const [open, setOpen] = useState(false);
  const [apiConfigured, setApiConfigured] = useState<boolean | null>(null);
  const lang = uiLang;
  const ref = useOverlayFocus(open, () => setOpen(false));

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
        aria-label={t("settings", lang)}
        title={t("settings", lang)}
        className="btn-ghost"
      >
        <Settings className="h-4 w-4" />
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div ref={ref} role="dialog" aria-modal="true" aria-label={t("settings", lang)} tabIndex={-1} className="settings-dialog fixed bottom-20 left-16 z-50 w-64 max-w-[calc(100vw-32px)] rounded-[9px] border border-border-light bg-surface p-4 shadow-lg max-lg:left-4">
            {/* Language */}
            <div className="mb-4">
              <label className="label-text mb-1 flex items-center gap-1.5">
                <Globe className="h-3.5 w-3.5" />
                {t("settings_language", lang)}
              </label>
              <div className="grid grid-cols-2 gap-2">
                <button
                  onClick={() => setUiLang("en")}
                  className={`flex items-center justify-center gap-1.5 rounded-[7px] px-3 py-2 text-[12px] font-medium ${
                    uiLang === "en" ? "bg-accent text-white" : "border border-border hover:bg-surface-hover hover:text-accent"
                  }`}
                >
                  {uiLang === "en" && <Check className="h-3 w-3" />}
                  English
                </button>
                <button
                  onClick={() => setUiLang("zh")}
                  className={`flex items-center justify-center gap-1.5 rounded-[7px] px-3 py-2 text-[12px] font-medium ${
                    uiLang === "zh" ? "bg-accent text-white" : "border border-border hover:bg-surface-hover hover:text-accent"
                  }`}
                >
                  {uiLang === "zh" && <Check className="h-3 w-3" />}
                  中文
                </button>
              </div>
            </div>

            <hr className="my-3 border-border-light" />

            {/* API Key */}
            <div className="mb-4">
              <label className="label-text mb-1 flex items-center gap-1.5">
                <Key className="h-3.5 w-3.5" />
                {t("settings_api_key", lang)}
              </label>
              {apiConfigured === null ? (
                <p className="hint-text">…</p>
              ) : apiConfigured ? (
                <p className="hint-text text-success">{t("settings_api_configured", lang)}</p>
              ) : (
                <p className="hint-text">{t("settings_api_not_configured", lang)}</p>
              )}
            </div>

            <hr className="my-3 border-border-light" />

            {/* Data source attribution (CC-BY / CC-BY-SA obligations) */}
            <div>
              <label className="label-text mb-1 block">
                {t("settings_data_sources", lang)}
              </label>
              <p className="text-[12px] leading-relaxed text-muted">
                {t("settings_data_sources_body", lang)}
              </p>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
