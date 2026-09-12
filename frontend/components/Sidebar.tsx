"use client";
import { useState, useEffect } from "react";
import { Plus, Clock, Pencil, Check, X, FileText, Map, MessageSquare, PanelLeftClose } from "lucide-react";
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
    <aside className="history-panel flex h-full w-[224px] shrink-0 flex-col border-r border-border-light bg-surface">
      {/* Header: collapse + history caption (brand lives in the app rail) */}
      <div className="flex h-[52px] shrink-0 items-center gap-2 border-b border-border-light px-3">
        <Clock className="h-3 w-3 text-muted" />
        <h3 className="text-[12px] font-semibold uppercase tracking-[0.15em] text-muted">
          {t("sidebar_history", lang)}
        </h3>
        {onCollapse && (
          <button
            onClick={onCollapse}
            className="ml-auto flex h-6 w-6 items-center justify-center rounded-[7px] text-muted transition-colors hover:bg-surface-hover hover:text-fg"
            title="收起左边栏"
          >
            <PanelLeftClose className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      {/* New session */}
      <div className="px-3 py-2.5">
        <button
          onClick={onNewSession}
          className="flex w-full items-center gap-2 rounded-[7px] border border-border-light px-3 py-2 text-[13px] font-medium text-fg-secondary transition-colors hover:border-border hover:bg-surface-hover/60 hover:text-fg"
        >
          <Plus className="h-3.5 w-3.5" />
          {lang === "zh" ? "新对话" : "New Chat"}
        </button>
      </div>

      {/* History */}
      <div className="flex-1 overflow-y-auto px-2 pb-4">
        <div className="space-y-0.5">
          {historyList.length === 0 ? (
            <p className="px-3 py-4 text-center text-[12px] text-muted">
              {t("sidebar_no_history", lang)}
            </p>
          ) : (
            historyList.map(h => {
              const active = currentChatFilename === h.filename;
              return (
                <div
                  key={h.filename}
                  tabIndex={0}
                  role="button"
                  aria-current={active ? "page" : undefined}
                  onKeyDown={e => { if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); onSelectHistory(h.filename); } }}
                  className={`history-row group flex cursor-pointer items-center gap-2.5 rounded-[7px] px-3 py-2 transition-colors hover:bg-surface-hover/60 ${active ? "bg-accent-soft/60" : ""}`}
                  onClick={() => onSelectHistory(h.filename)}
                >
                  <MessageSquare className={`h-3.5 w-3.5 shrink-0 ${active ? "text-accent/70" : "text-muted"}`} />
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
                        <span className={`block truncate text-[13px] group-hover:text-fg ${active ? "font-medium text-accent" : "text-fg-secondary"}`}>
                          {h.title || h.topic?.slice(0, 24) || "新对话"}
                        </span>
                        <div className="flex items-center gap-1.5">
                          {h.timestamp && (
                            <span className="tnum text-[12px] text-muted">
                              {h.timestamp.slice(0, 8)}
                            </span>
                          )}
                          {!!h.paper_count && (
                            <span className="tnum inline-flex items-center gap-0.5 text-[12px] text-muted">
                              <FileText className="h-2.5 w-2.5" />{h.paper_count}
                            </span>
                          )}
                          {h.has_map && (
                            <Map className="h-2.5 w-2.5 text-muted" />
                          )}
                        </div>
                      </>
                    )}
                  </div>
                  {editing !== h.filename && (
                    <div className="flex shrink-0 items-center opacity-0 transition-all group-hover:opacity-100">
                      <button
                        onClick={e => { e.stopPropagation(); startRename(h.filename, h.title || h.topic || "新对话"); }}
                        className="rounded p-1 text-muted hover:text-accent"
                        title="重命名"
                      >
                        <Pencil className="h-3 w-3" />
                      </button>
                      <button
                        onClick={e => { e.stopPropagation(); handleDelete(h.filename); }}
                        className="rounded p-1 text-muted hover:text-error"
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
