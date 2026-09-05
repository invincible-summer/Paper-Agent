"use client";
import { OpenReaderButton } from "@/components/reader/OpenReaderButton";
import { useEffect, useMemo, useState } from "react";
import {
  PanelRightClose, FileText, Files, Loader2, FileWarning, ExternalLink,
  Image as ImageIcon,
} from "lucide-react";
import { useChatStore } from "@/stores/chat";
import { useUIStore } from "@/stores/ui";
import {
  attachmentMetaLabel,
  fetchFileBlob,
  fetchFileContent,
  isImageAttachment,
  type ChatAttachment,
  type ChatFileContent,
} from "@/lib/chat-api";

// D-091: right sidebar with two tabs. Protected image originals are fetched
// as blobs (so guest/auth headers are retained); documents show extracted or
// multimodally enriched text from GET /chat/file/{id}.

export function RightSidebar() {
  const { rightPanelTab, setRightPanelTab, setRightSidebarOpen,
    activeFileId, activeFileMeta } = useUIStore();
  const messages = useChatStore(s => s.messages);
  const sessionAttachments = useChatStore(s => s.sessionAttachments);

  // Deduped upload list. Later per-message metadata may be fresher than old
  // history/session metadata, so merge rather than discarding duplicate ids.
  const files: ChatAttachment[] = useMemo(() => {
    const seen = new Map<string, ChatAttachment>();
    for (const a of sessionAttachments) {
      if (a.id) seen.set(a.id, { ...a });
    }
    for (const m of messages) {
      if (m.role !== "user" || !m.attachments) continue;
      for (const a of m.attachments) {
        if (!a.id) continue;
        seen.set(a.id, { ...(seen.get(a.id) || {}), ...a });
      }
    }
    return Array.from(seen.values());
  }, [messages, sessionAttachments]);

  const selectedMeta = activeFileId
    ? files.find((f) => f.id === activeFileId) || activeFileMeta
    : activeFileMeta;

  return (
    <aside className="flex h-full w-[340px] shrink-0 flex-col border-l border-border-light bg-surface/50">
      <div className="flex items-center gap-1 px-2 py-2.5">
        <TabButton active={rightPanelTab === "viewer"} onClick={() => setRightPanelTab("viewer")}>
          {selectedMeta && isImageAttachment(selectedMeta)
            ? <ImageIcon className="h-3.5 w-3.5" />
            : <FileText className="h-3.5 w-3.5" />}
          {selectedMeta?.filename ? "文件预览" : "预览"}
        </TabButton>
        <TabButton active={rightPanelTab === "list"} onClick={() => setRightPanelTab("list")}>
          <Files className="h-3.5 w-3.5" />
          {`上传列表${files.length ? ` (${files.length})` : ""}`}
        </TabButton>
        <button
          onClick={() => setRightSidebarOpen(false)}
          className="ml-auto flex h-7 w-7 items-center justify-center rounded-lg text-muted transition-colors hover:bg-surface-hover hover:text-fg"
          title="收起右边栏"
        >
          <PanelRightClose className="h-4 w-4" />
        </button>
      </div>

      <div className="flex-1 overflow-hidden">
        {rightPanelTab === "viewer" ? (
          <FileViewer fileMeta={selectedMeta} />
        ) : (
          <FileList files={files} activeId={activeFileId} />
        )}
      </div>
    </aside>
  );
}

function TabButton({ active, onClick, children }: {
  active: boolean; onClick: () => void; children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[12px] font-medium transition-colors ${
        active ? "bg-surface text-fg shadow-sm" : "text-muted hover:text-fg-secondary hover:bg-surface-hover/60"
      }`}
    >
      {children}
    </button>
  );
}

// Text responses are safe to retain; object URLs are intentionally per-view
// and revoked by the image preview effect.
const fileCache = new Map<string, ChatFileContent>();

function FileViewer({ fileMeta }: { fileMeta: ChatAttachment | null }) {
  const fileId = fileMeta?.id || null;
  const image = Boolean(fileMeta && isImageAttachment(fileMeta));
  const [data, setData] = useState<ChatFileContent | null>(null);
  const [imageUrl, setImageUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    setData(null);
    setImageUrl(null);
    setError(null);
    if (!fileId || !fileMeta) {
      setLoading(false);
      return () => {};
    }

    setLoading(true);
    if (image) {
      const rawUrl = fileMeta.preview_url || `/api/v1/chat/file/${encodeURIComponent(fileId)}/raw`;
      fetchFileBlob(rawUrl)
        .then((blob) => {
          if (cancelled) return;
          objectUrl = URL.createObjectURL(blob);
          setImageUrl(objectUrl);
        })
        .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)); })
        .finally(() => { if (!cancelled) setLoading(false); });
    } else {
      const cached = fileCache.get(fileId);
      if (cached) {
        setData(cached);
        setLoading(false);
      } else {
        fetchFileContent(fileId)
          .then((d) => {
            if (cancelled) return;
            fileCache.set(fileId, d);
            setData(d);
          })
          .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)); })
          .finally(() => { if (!cancelled) setLoading(false); });
      }
    }

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [fileId, fileMeta, image]);

  if (!fileId || !fileMeta) {
    return (
      <div className="flex h-full flex-col items-center justify-center px-6 text-center">
        <FileText className="mb-3 h-8 w-8 text-muted/30" />
        <p className="text-[13px] text-muted">点击对话中的文件图标</p>
        <p className="mt-1 text-[11px] text-muted/60">或从上传列表选择文件</p>
      </div>
    );
  }
  if (loading) {
    return <div className="flex h-full items-center justify-center"><Loader2 className="h-5 w-5 animate-spin text-accent" /></div>;
  }
  if (error) {
    return (
      <div className="flex h-full flex-col items-center justify-center px-6 text-center">
        <FileWarning className="mb-3 h-8 w-8 text-red-400/60" />
        <p className="text-[13px] text-muted">无法加载文件</p>
        <p className="mt-1 text-[11px] text-muted/60">{error}</p>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-border-light px-3 py-2.5">
        <div className="flex items-center gap-2">
          {image ? <ImageIcon className="h-3.5 w-3.5 shrink-0 text-accent" /> : <FileText className="h-3.5 w-3.5 shrink-0 text-accent" />}
          <span className="truncate text-[13px] font-semibold text-fg" title={fileMeta.filename}>{fileMeta.filename}</span>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px] text-muted">
          <span>{attachmentMetaLabel(fileMeta)}</span>
          <OpenReaderButton attachment={fileMeta} />
          {!image && data?.truncated && <span className="text-amber-500/80">（已截断）</span>}
        </div>
      </div>
      {image ? (
        <div className="flex flex-1 items-center justify-center overflow-auto bg-surface-hover/20 p-3">
          {imageUrl ? (
            // eslint-disable-next-line @next/next/no-img-element -- authenticated blob URL cannot use Next image optimization.
            <img src={imageUrl} alt={fileMeta.filename} className="max-h-full max-w-full rounded-lg object-contain shadow-sm" />
          ) : <p className="text-[12px] text-muted">图片预览不可用</p>}
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto px-3.5 py-3">
          <pre className="whitespace-pre-wrap break-words font-sans text-[12.5px] leading-[1.7] text-fg-secondary">
{data?.text || "（未提取到可显示文本）"}
          </pre>
        </div>
      )}
    </div>
  );
}

function FileList({ files, activeId }: { files: ChatAttachment[]; activeId: string | null }) {
  const { openFile, setRightPanelTab } = useUIStore();
  if (files.length === 0) {
    return (
      <div className="flex h-full flex-col items-center justify-center px-6 text-center">
        <Files className="mb-3 h-8 w-8 text-muted/30" />
        <p className="text-[13px] text-muted">当前会话暂无上传文件</p>
        <p className="mt-1 text-[11px] text-muted/60">支持 PDF、DOCX、TEX、TXT、MD、BIB 及 PNG/JPG/WebP</p>
      </div>
    );
  }
  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-border-light px-3 py-2 text-[10px] font-semibold uppercase tracking-wider text-muted">本会话上传文件</div>
      <div className="flex-1 overflow-y-auto px-2 py-2">
        <div className="space-y-1">
          {files.map((f) => {
            const active = f.id === activeId;
            const image = isImageAttachment(f);
            const Icon = image ? ImageIcon : FileText;
            return (
              <div key={f.id}>
              <button
                onClick={() => { openFile(f); setRightPanelTab("viewer"); }}
                className={`flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-surface-hover/60 ${active ? "bg-surface-hover/60" : ""}`}
              >
                <Icon className={`h-3.5 w-3.5 shrink-0 ${active ? "text-accent" : "text-muted/50"}`} />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-[12.5px] text-fg-secondary" title={f.filename}>{f.filename}</div>
                  <div className="text-[10px] text-muted/60">{attachmentMetaLabel(f)}</div>
                </div>
                <ExternalLink className="h-3 w-3 shrink-0 text-muted/30" />
              </button>
              <div className="px-3 pb-2"><OpenReaderButton attachment={f} /></div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
