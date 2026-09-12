"use client";
import { useRef, useEffect, useState } from "react";
import { ArrowUp, Square, Paperclip, X, FileText, Image as ImageIcon, Loader2 } from "lucide-react";
import { OpenReaderButton } from "@/components/reader/OpenReaderButton";
import { useChatStore } from "@/stores/chat";
import { useUIStore } from "@/stores/ui";

import type { ChatAttachment } from "@/lib/chat-api";
import { attachmentMetaLabel, isImageAttachment, uploadFiles } from "@/lib/chat-api";

export function ChatInput({
  onSend,
  disabled,
  onStop,
}: {
  onSend: (message: string, attachments?: ChatAttachment[]) => void;
  disabled: boolean;
  // D-088: onStop fires when the user clicks the stop button to interrupt an
  // in-progress turn. Optional so the component stays usable without it.
  onStop?: () => void;
}) {
  const [text, setText] = useState("");
  // D-087: file upload state. `pending` are File objects the user picked but
  // haven't been uploaded yet; `uploaded` carry the server-returned metadata.
  const [pending, setPending] = useState<File[]>([]);
  const [uploaded, setUploaded] = useState<ChatAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const ta = textareaRef.current;
    if (ta) {
      ta.style.height = "auto";
      ta.style.height = Math.min(ta.scrollHeight, 180) + "px";
    }
  }, [text]);

  // Consume one-shot composer drafts (e.g. 「深问这篇」from the genealogy graph).
  const composerDraft = useUIStore((st) => st.composerDraft);
  const setComposerDraft = useUIStore((st) => st.setComposerDraft);
  useEffect(() => {
    if (!composerDraft) return;
    setText(composerDraft);
    setComposerDraft("");
    textareaRef.current?.focus();
  }, [composerDraft, setComposerDraft]);

  const addFiles = (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setUploadError(null);
    // D-087: client-side filter must stay in sync with the <input accept> set
    // AND backend ingestion. Supported: documents plus PNG/JPEG/WebP. Upload
    // itself is lightweight; multimodal understanding remains on demand.
    // Legacy .doc (Word binary) has no parser in this env → guide the
    // user to save-as .docx instead of silently failing extraction.
    const supported = /\.(pdf|docx|tex|txt|md|markdown|bib|png|jpe?g|webp)$/i;
    const accepted: File[] = [];
    let docHint = false;
    let rejected = 0;
    for (const f of Array.from(files)) {
      if (/\.doc$/i.test(f.name)) {
        docHint = true;
      } else if (supported.test(f.name) || ["application/pdf", "image/png", "image/jpeg", "image/webp"].includes(f.type)) {
        accepted.push(f);
      } else {
        rejected += 1;
      }
    }
    const parts: string[] = [];
    if (docHint) parts.push("旧版 .doc 无法解析，请在 Word 里另存为 .docx 后重传");
    if (rejected > 0) parts.push(`${rejected} 个文件格式不支持（支持 PDF / Word(.docx) / TeX / TXT / MD / BIB / PNG / JPG / WebP）`);
    if (parts.length) setUploadError(parts.join("；"));
    if (accepted.length === 0) return;
    setPending((p) => [...p, ...accepted]);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    if (disabled || uploading) return;
    addFiles(e.dataTransfer.files);
  };

  const removePending = (idx: number) =>
    setPending((p) => p.filter((_, i) => i !== idx));
  const removeUploaded = (idx: number) =>
    setUploaded((u) => u.filter((_, i) => i !== idx));

  const handleSubmit = async () => {
    if (disabled) return;
    // Upload any pending files first, then send with all attachments.
    let allAttachments = [...uploaded];
    if (pending.length > 0) {
      setUploading(true);
      try {
        const ups = await uploadFiles(pending);
        const failed = ups.filter((u) => u.error);
        if (failed.length > 0) {
          setUploadError(failed.map((f) => `${f.filename}: ${f.error}`).join("；"));
        }
        allAttachments = [...allAttachments, ...ups.filter((u) => !u.error)];
      } catch (e) {
        setUploadError(e instanceof Error ? e.message : String(e));
        setUploading(false);
        return;
      }
      setUploading(false);
    }
    const trimmed = text.trim();
    if (!trimmed && allAttachments.length === 0) return;
    onSend(trimmed, allAttachments.length > 0 ? allAttachments : undefined);
    setText("");
    setPending([]);
    setUploaded([]);
    setUploadError(null);
  };

  const prepareReading = async () => {
    setUploading(true); setUploadError(null);
    try {
      const result = await uploadFiles(pending);
      const ok = result.filter(a => !a.error);
      setUploaded(u => [...u, ...ok]);
      setPending(files => files.filter((_file, index) => result[index]?.error));
      const chat = useChatStore.getState();
      chat.setSessionAttachments([...chat.sessionAttachments, ...ok]);
      const failures = result.filter(a => a.error);
      if (failures.length) setUploadError(failures.map(a => `${a.filename}: ${a.error}`).join("；"));
    } catch (e) { setUploadError(e instanceof Error ? e.message : "上传失败"); }
    finally { setUploading(false); }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void handleSubmit();
    }
  };

  const canSend = !disabled && !uploading &&
    (text.trim().length > 0 || pending.length > 0 || uploaded.length > 0);
  const openFile = useUIStore((st) => st.openFile);
  const allChips = [
    ...uploaded.map((a, i) => ({
      kind: "done" as const,
      name: a.filename,
      idx: i,
      id: a.id,
      meta: attachmentMetaLabel(a),
      image: isImageAttachment(a),
    })),
    ...pending.map((f, i) => ({
      kind: "pending" as const,
      name: f.name,
      idx: i,
      meta: `${Math.max(1, Math.round(f.size / 1024))}KB`,
      image: f.type.startsWith("image/") || /\.(png|jpe?g|webp)$/i.test(f.name),
    })),
  ];

  return (
    <div className="composer">
      <div className="mx-auto max-w-[820px]">
        <div
          onDragOver={(e) => { e.preventDefault(); if (!disabled && !uploading) setDragOver(true); }}
          onDragLeave={(e) => { if (e.currentTarget === e.target) setDragOver(false); }}
          onDrop={handleDrop}
          className={`composer-box flex items-end gap-2 rounded-[9px] border bg-surface px-3.5 py-3 transition-colors focus-within:border-accent/50 ${dragOver ? "border-accent/60 bg-accent-soft/20" : "border-border"}`}
        >
          {/* D-087: paperclip attach button */}
          <button
            onClick={() => fileRef.current?.click()}
            disabled={disabled || uploading}
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-muted transition-colors hover:bg-surface-hover hover:text-fg disabled:opacity-40"
            title="上传文件（PDF / Word(.docx) / TeX / TXT / MD / BIB / PNG / JPG / WebP），也可拖入"
          >
            {uploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Paperclip className="h-4 w-4" />}
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".pdf,.txt,.md,.markdown,.bib,.docx,.tex,.doc,.png,.jpg,.jpeg,.webp,application/pdf,text/plain,text/markdown,image/png,image/jpeg,image/webp"
            multiple
            className="hidden"
            onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }}
          />
          <textarea
            ref={textareaRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="输入你的问题..."
            disabled={disabled}
            rows={1}
            className="flex-1 resize-none bg-transparent text-[14px] leading-[1.6] text-fg outline-none placeholder:text-muted disabled:opacity-50"
            style={{ maxHeight: "180px" }}
          />
          {/* D-088: the send button doubles as the stop button while a turn
              is streaming — same slot, no separate control. */}
          {disabled && onStop ? (
            <button
              onClick={onStop}
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent text-white transition-all hover:bg-error shadow-sm"
              title="停止生成"
            >
              <Square className="h-3.5 w-3.5 fill-current" />
            </button>
          ) : (
          <button
            onClick={() => void handleSubmit()}
            aria-label="发送消息"
            disabled={!canSend}
            className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full transition-all ${
              canSend
                ? "bg-accent text-white hover:bg-accent-hover shadow-sm"
                : "bg-surface-hover/60 text-muted"
            }`}
          >
            {uploading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : disabled ? <Square className="h-3.5 w-3.5" /> : <ArrowUp className="h-4 w-4" />}
          </button>
          )}
        </div>
        {/* Attachment chips + upload errors (D-087) */}
        {allChips.length > 0 && (
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {allChips.map((c) => {
              const clickable = c.kind === "done";
              return (
              <span key={`${c.kind}-${c.idx}`} className={`flex items-center gap-1.5 rounded-[7px] border border-border-light bg-surface px-2 py-1 text-[12px] text-fg-secondary ${clickable ? "cursor-pointer transition-colors hover:border-accent/30 hover:bg-surface-hover/60 hover:text-fg" : ""}`}
                title={clickable ? `${c.name} — 点击在右侧${c.image ? "预览图片" : "查看提取文本"}` : c.name}
                onClick={() => { if (!clickable) return; const a = uploaded[c.idx]; if (a) openFile(a); }}
              >
                {c.image ? <ImageIcon className="h-3 w-3 text-accent" /> : <FileText className="h-3 w-3 text-accent" />}
                <span className="max-w-[180px] truncate">{c.name}</span>
                <span className="text-muted">{c.meta}</span>
                <button onClick={(e) => { e.stopPropagation(); c.kind === "done" ? removeUploaded(c.idx) : removePending(c.idx); }} className="text-muted hover:text-error" title="移除">
                  <X className="h-3 w-3" />
                </button>
              </span>
              );
            })}
          </div>
        )}
        {(pending.some(f => /\.pdf$/i.test(f.name)) || uploaded.some(a => /\.pdf$/i.test(a.filename))) && (
          <div className="mt-2 flex flex-wrap items-center gap-2 text-[12px] text-muted">
            {pending.length > 0 && <button type="button" disabled={disabled || uploading} onClick={() => void prepareReading()} className="rounded-md border border-accent/20 px-3 py-1 text-accent">{uploading ? "正在准备…" : "只上传，开始阅读"}</button>}
            {uploaded.map(a => <OpenReaderButton key={a.id} attachment={a} />)}
          </div>
        )}
        {uploadError && <p className="mt-1 text-[12px] text-error/70">{uploadError}</p>}
        <p className="mt-2 flex flex-wrap justify-between gap-1 text-[12px] text-muted">
          <span>Enter 发送 · Shift + Enter 换行</span><span>请以原始文献核实 AI 生成内容</span>
        </p>
      </div>
    </div>
  );
}
