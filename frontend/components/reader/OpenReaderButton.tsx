"use client";
import { useState } from "react";
import { BookOpen, Loader2 } from "lucide-react";
import { useChatStore } from "@/stores/chat";
import { openReader, readerHref } from "@/lib/reader-api";
import type { ChatAttachment } from "@/lib/chat-api";

export function OpenReaderButton({ attachment, compact = false }: { attachment: ChatAttachment; compact?: boolean }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (!/\.pdf$/i.test(attachment.filename) && attachment.ext !== "pdf") return null;
  const open = async () => {
    const state = useChatStore.getState();
    if (state.isResponding && !state.currentChatFilename) { setError("本轮回复结束后即可打开工作台"); return; }
    // 同步开窗保留主聊天流式轮次和草稿，也避免异步请求后被弹窗拦截。
    const target = window.open("about:blank", "_blank");
    if (!target) { setError("请允许此站点打开阅读标签页，再试一次"); return; }
    target.document.title = "正在打开阅研…";
    target.document.body.textContent = "正在准备阅读工作台…";
    setBusy(true); setError("");
    try {
      // history_filename 可为空：后端会复用该论文已有的阅读会话（进度/笔记保留）。
      const bound = await openReader(attachment.id, state.currentChatFilename);
      const current = useChatStore.getState();
      if (current.currentChatFilename === state.currentChatFilename && (state.currentChatFilename || current.messages === state.messages)) {
        if (!state.currentChatFilename) current.setCurrentChatFilename(bound.history_filename);
        if (!current.sessionAttachments.some(a => a.id === attachment.id)) current.setSessionAttachments([...current.sessionAttachments, attachment]);
      }
      target.location.href = readerHref(bound.session_id, attachment.id);
    } catch (e) { target.close(); setError(e instanceof Error ? e.message : "无法打开工作台"); }
    finally { setBusy(false); }
  };
  return <span className="inline-flex flex-col gap-1">
    <button type="button" title="在新标签页打开原版 PDF 阅读工作台" disabled={busy} onClick={e => { e.stopPropagation(); void open(); }}
      className="inline-flex items-center justify-center gap-1.5 rounded-md border border-accent/20 bg-accent-soft/40 px-2 py-1 text-[11px] text-accent hover:bg-accent-soft disabled:opacity-50">
      {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <BookOpen className="h-3 w-3" />}{compact ? "阅研" : "打开阅读工作台"}
    </button>{error && <small role="alert" className="max-w-[260px] text-[10px] text-error">{error}</small>}
  </span>;
}
