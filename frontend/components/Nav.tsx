"use client";
import { BookOpen, BookOpenText, LogOut, ShieldCheck } from "lucide-react";
import { useRouter } from "next/navigation";
import { useChatStore } from "@/stores/chat";
import { useAuthStore } from "@/stores/auth";
import { apiLogout } from "@/lib/auth";
import { t } from "@/lib/i18n";
import { SettingsPopover } from "./SettingsPopover";

export function Nav() {
  const { uiLang: lang } = useChatStore();
  const authRequired = useAuthStore((s) => s.authRequired);
  const user = useAuthStore((s) => s.user);
  const guestAccess = useAuthStore((s) => s.guestAccess);
  const registrationOpen = useAuthStore((s) => s.registrationOpen);
  const signOut = useAuthStore((s) => s.signOut);
  const router = useRouter();

  const handleLogout = async () => {
    await apiLogout();
    signOut();
    router.replace(guestAccess ? "/chat" : "/login");
    router.refresh();
  };

  return (
    <header className="sticky top-0 z-30 flex items-center justify-between border-b border-border-light bg-surface/80 px-6 py-2.5 backdrop-blur-xl">
      <div className="flex items-center gap-2.5">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-accent to-accent-hover shadow-sm">
          <BookOpen className="h-4 w-4 text-white" />
        </div>
        <div>
          <h1 className="text-[15px] font-bold leading-none tracking-tight">
            {t("app_title", lang)}
          </h1>
          <p className="mt-1 text-[11px] leading-none text-muted">{t("app_subtitle", lang)}</p>
        </div>
      </div>
      <div className="flex items-center gap-1.5">
        <button
          onClick={() => router.push("/usage-doc")}
          title="使用文档"
          className="flex h-8 items-center gap-1.5 rounded-lg px-3 text-[13px] text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg"
        >
          <BookOpenText className="h-4 w-4" />
          使用文档
        </button>
        {authRequired && (user ? (
          <div className="flex items-center gap-1.5">
            {user.role === "administrator" && (
              <button
                onClick={() => router.push("/admin/auth-settings")}
                title="管理后台"
                className="flex h-8 items-center gap-1.5 rounded-lg px-3 text-[13px] text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg"
              >
                <ShieldCheck className="h-4 w-4" />
                管理
              </button>
            )}
            <span className="flex h-8 items-center rounded-full bg-surface-hover px-3 text-[13px] text-fg-secondary">
              {user.display_name || user.username}
            </span>
            <button
              onClick={handleLogout}
              title={guestAccess ? "退出登录（回到游客模式）" : "退出登录"}
              className="flex h-8 w-8 items-center justify-center rounded-lg text-muted transition-colors hover:bg-surface-hover hover:text-fg"
            >
              <LogOut className="h-4 w-4" />
            </button>
          </div>
        ) : (
          <div className="flex items-center gap-1.5">
            <span className="flex h-8 items-center rounded-full border border-dashed border-border px-3 text-[13px] text-muted">
              {guestAccess ? "游客" : "未登录"}
            </span>
            <button
              onClick={() => router.push("/login")}
              className="flex h-8 items-center rounded-lg bg-accent px-3.5 text-[13px] font-medium text-white shadow-sm transition-all hover:bg-accent-hover"
            >
              {registrationOpen ? "登录 / 注册" : "登录"}
            </button>
          </div>
        ))}
        <SettingsPopover />
      </div>
    </header>
  );
}
