"use client";
import { useState, useEffect } from "react";
import { Plus, Clock, Sparkles, Pencil, Check, X, FileText, Map, MessageSquare, PanelLeftClose } from "lucide-react";
import { useChatStore } from "@/stores/chat";
import { t } from "@/lib/i18n";
import { listChatHistory, deleteChatHistory, renameChatHistory } from "@/lib/chat-api";

export function Sidebar({
  onNewSession,
  onSelectHistory,
  onCollapse,
}: {
  onNewSession: () => void;
  onSelectHistory: (filename: string) => void;
  onCollapse?: () => void;
}) {
  const { uiLang: lang, historyList, currentChatFilename, setHistoryList, deleteHistory, renameHistory } = useChatStore();
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  useEffect(() => {
    listChatHistory().then(r => setHistoryList(r.records)).catch(() => {});
  }, [setHistoryList]);

  const startRename = (filename: string, currentTitle: string) => {
    setEditing(filename);
    setDraft(currentTitle);
  };
  const commitRename = async (filename: string) => {
    const title = draft.trim();
    if (title) {
      try { await renameChatHistory(filename, title); } catch {}
      renameHistory(filename, title);
    }
    setEditing(null);
  };
  const handleDelete = async (filename: string) => {
    try { await deleteChatHistory(filename); } catch {}
    deleteHistory(filename);
  };

  return (
    <aside className="flex h-full w-[264px] shrink-0 flex-col border-r border-border-light bg-surface/50">
      {/* Brand */}
      <div className="flex items-center gap-2 px-4 py-3.5">
        <div className="flex h-6 w-6 items-center justify-center rounded-lg bg-accent-soft/50">
          <Sparkles className="h-3.5 w-3.5 text-accent" />
        </div>
        <span className="font-serif-display text-[13px] font-bold text-fg">Paper Agent</span>
        {onCollapse && (
          <button
            onClick={onCollapse}
            className="ml-auto flex h-6 w-6 items-center justify-center rounded-lg text-muted transition-colors hover:bg-surface-hover hover:text-fg"
            title="收起左边栏"
          >
            <PanelLeftClose className="h-4 w-4" />
          </button>
        )}
      </div>

      {/* New session */}
      <div className="px-3 pb-2">
        <button
          onClick={onNewSession}
          className="flex w-full items-center gap-2 rounded-xl border border-border-light px-3.5 py-2.5 text-[13px] font-medium text-fg-secondary transition-all hover:border-border hover:bg-surface-hover/60 hover:text-fg"
        >
          <Plus className="h-4 w-4" />
          {lang === "zh" ? "新对话" : "New Chat"}
        </button>
      </div>

      {/* History */}
      <div className="flex-1 overflow-y-auto px-2 pb-4">
        <div className="mb-2 flex items-center gap-1.5 px-2 pt-2">
          <Clock className="h-3 w-3 text-muted" />
          <h3 className="text-[10px] font-semibold uppercase tracking-wider text-muted">
            {t("sidebar_history", lang)}
          </h3>
        </div>
        <div className="mb-4 space-y-0.5">
          {historyList.length === 0 ? (
            <p className="px-3 py-4 text-center text-xs text-muted/50">
              {t("sidebar_no_history", lang)}
            </p>
          ) : (
            historyList.map(h => {
              const active = currentChatFilename === h.filename;
              return (
                <div
                  key={h.filename}
                  className={`group flex cursor-pointer items-center gap-2.5 rounded-lg px-3 py-2 transition-colors hover:bg-surface-hover/50 ${active ? "bg-surface-hover/50" : ""}`}
                  onClick={() => onSelectHistory(h.filename)}
                >
                  <MessageSquare className="h-3.5 w-3.5 shrink-0 text-muted/40" />
                  <div className="min-w-0 flex-1">
                    {editing === h.filename ? (
                      <div className="flex items-center gap-1" onClick={e => e.stopPropagation()}>
                        <input
                          autoFocus
                          value={draft}
                          onChange={e => setDraft(e.target.value)}
                          onKeyDown={e => {
                            if (e.key === "Enter") commitRename(h.filename);
                            if (e.key === "Escape") setEditing(null);
                          }}
                          className="w-full rounded border border-accent/40 bg-bg px-1.5 py-0.5 text-[12px] text-fg outline-none"
                        />
                        <button onClick={() => commitRename(h.filename)} className="rounded p-0.5 text-accent hover:bg-accent-soft/40">
                          <Check className="h-3 w-3" />
                        </button>
                        <button onClick={() => setEditing(null)} className="rounded p-0.5 text-muted hover:bg-surface-hover/60">
                          <X className="h-3 w-3" />
                        </button>
                      </div>
                    ) : (
                      <>
                        <span className="block truncate text-[13px] text-fg-secondary group-hover:text-fg">
                          {h.title || h.topic?.slice(0, 24) || "新对话"}
                        </span>
                        <div className="flex items-center gap-1.5">
                          {h.timestamp && (
                            <span className="tnum text-[10px] text-muted/40">
                              {h.timestamp.slice(0, 8)}
                            </span>
                          )}
                          {!!h.paper_count && (
                            <span className="tnum inline-flex items-center gap-0.5 text-[10px] text-muted/40">
                              <FileText className="h-2.5 w-2.5" />{h.paper_count}
                            </span>
                          )}
                          {h.has_map && (
                            <Map className="h-2.5 w-2.5 text-muted/40" />
                          )}
                        </div>
                      </>
                    )}
                  </div>
                  {editing !== h.filename && (
                    <div className="flex shrink-0 items-center opacity-0 transition-all group-hover:opacity-100">
                      <button
                        onClick={e => { e.stopPropagation(); startRename(h.filename, h.title || h.topic || "新对话"); }}
                        className="rounded p-1 text-muted/30 hover:text-accent"
                        title="重命名"
                      >
                        <Pencil className="h-3 w-3" />
                      </button>
                      <button
                        onClick={e => { e.stopPropagation(); handleDelete(h.filename); }}
                        className="rounded p-1 text-muted/30 hover:text-error/70"
                        title="删除"
                      >
                        <X className="h-3 w-3" />
                      </button>
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>
      </div>
    </aside>
  );
}
