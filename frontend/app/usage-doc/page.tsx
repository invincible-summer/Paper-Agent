"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type ComponentPropsWithoutRef } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { ArrowLeft, BookOpenText, ImagePlus, Loader2, Save } from "lucide-react";
import { useRouter } from "next/navigation";
import { AppFrame } from "@/components/AppFrame";
import { EmptyState, PageHeader, SectionPanel, StatusNotice } from "@/components/WorkbenchUI";
import {
  extractUsageDocOutline,
  UsageDocMobileOutline,
  UsageDocOutline,
  type UsageDocOutlineItem,
} from "@/components/UsageDocOutline";
import { useAuthStore } from "@/stores/auth";
import {
  getUsageDocument,
  updateUsageDocument,
  uploadUsageDocumentImage,
  UsageDocumentApiError,
  type UsageDocument,
} from "@/lib/usage-document-api";

const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const ACCEPTED_IMAGE_TYPES = "image/png,image/jpeg,image/gif,image/webp";

function formatUpdatedAt(timestamp: number): string {
  if (!timestamp) return "尚未更新";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(timestamp * 1000));
}

type HeadingNode = { position?: { start?: { line?: number } } };
type HeadingProps = ComponentPropsWithoutRef<"h1"> & { node?: HeadingNode };

function MarkdownContent({
  content,
  outline,
}: {
  content: string;
  outline: UsageDocOutlineItem[];
}) {
  const lineToItem = useMemo(() => {
    const map = new Map<number, UsageDocOutlineItem>();
    for (const item of outline) map.set(item.line, item);
    return map;
  }, [outline]);

  const components = useMemo<Components>(() => {
    // 锚点 id 由行号映射挂到标题上，与左侧目录共享同一份 outline。
    const heading = (tag: "h1" | "h2" | "h3" | "h4" | "h5" | "h6") => {
      const Heading = ({ node, children, ...props }: HeadingProps) => {
        const line = node?.position?.start?.line;
        const item = line !== undefined ? lineToItem.get(line) : undefined;
        if (item?.isTitleHeading) return null;
        const Tag = tag;
        return (
          <Tag {...props} id={item?.id} className="scroll-mt-28">
            {children}
          </Tag>
        );
      };
      return Heading;
    };
    return {
      h1: heading("h1"),
      h2: heading("h2"),
      h3: heading("h3"),
      h4: heading("h4"),
      h5: heading("h5"),
      h6: heading("h6"),
      a: ({ node: _node, ...props }) => (
        <a {...props} target="_blank" rel="noopener noreferrer" />
      ),
      img: ({ node: _node, ...props }) => (
        // eslint-disable-next-line @next/next/no-img-element
        <img {...props} loading="lazy" alt={props.alt || "文档图片"} />
      ),
    };
  }, [lineToItem]);

  return (
    <div className="usage-document-prose">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        urlTransform={(url) => {
          const value = url.trim();
          if ((value.startsWith("/") && !value.startsWith("//")) || value.startsWith("#")) return value;
          if (/^https?:\/\//i.test(value)) return value;
          if (/^mailto:/i.test(value)) return value;
          return "";
        }}
        components={components}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

export default function UsageDocumentPage() {
  const router = useRouter();
  const checked = useAuthStore((state) => state.checked);
  const user = useAuthStore((state) => state.user);
  const isAdministrator = checked && user?.role === "administrator";
  const [document, setDocument] = useState<UsageDocument | null>(null);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const outline = useMemo(
    () => extractUsageDocOutline(document?.content ?? ""),
    [document?.content]
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const next = await getUsageDocument();
      setDocument(next);
      setDraft(next.content);
    } catch (err) {
      setError(err instanceof Error ? err.message : "使用文档加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleSave = async () => {
    if (!document || saving) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const next = await updateUsageDocument(document.version, draft);
      setDocument(next);
      setDraft(next.content);
      setNotice(`文档已保存 · ${formatUpdatedAt(next.updated_at)}`);
    } catch (err) {
      if (err instanceof UsageDocumentApiError && err.status === 409) {
        setError(`${err.message} 当前草稿未丢失，请刷新后确认最新版本。`);
      } else {
        setError(err instanceof Error ? err.message : "文档保存失败");
      }
    } finally {
      setSaving(false);
    }
  };

  const insertAtCursor = (text: string) => {
    const textarea = textareaRef.current;
    if (!textarea) {
      setDraft((current) => `${current}${current ? "\n\n" : ""}${text}`);
      return;
    }
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const before = draft.slice(0, start);
    const after = draft.slice(end);
    const prefix = before && !before.endsWith("\n") ? "\n\n" : "";
    const suffix = after && !after.startsWith("\n") ? "\n\n" : "";
    const next = `${before}${prefix}${text}${suffix}${after}`;
    setDraft(next);
    requestAnimationFrame(() => {
      const cursor = before.length + prefix.length + text.length;
      textarea.focus();
      textarea.setSelectionRange(cursor, cursor);
    });
  };

  const handleImageUpload = async (file: File) => {
    if (!isAdministrator) return;
    if (!file.type.startsWith("image/")) {
      setError("请选择 PNG、JPEG、GIF 或 WebP 图片。");
      return;
    }
    if (file.size > MAX_IMAGE_BYTES) {
      setError("图片不能超过 10 MB。");
      return;
    }
    setUploading(true);
    setError("");
    setNotice("");
    try {
      const result = await uploadUsageDocumentImage(file);
      insertAtCursor(result.markdown);
      setNotice("图片已上传，并已插入当前光标位置；请保存文档后发布。 ");
    } catch (err) {
      setError(err instanceof Error ? err.message : "图片上传失败");
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  return (
    <AppFrame>
      <div className="library-canvas !max-w-[1200px] flex-1 overflow-y-auto">
        <PageHeader eyebrow="Guide" title="使用文档" description={`智能体功能介绍、使用方法与注意事项${document ? ` · 最后更新：${formatUpdatedAt(document.updated_at)}` : ""}`} icon={<BookOpenText className="h-5 w-5" />} actions={<button type="button" onClick={() => router.push("/chat")} className="btn-secondary"><ArrowLeft className="h-3.5 w-3.5" />返回对话</button>} />

        {error && <div className="mb-4"><StatusNotice tone="error">{error}</StatusNotice></div>}
        {notice && <div className="mb-4"><StatusNotice tone="success">{notice}</StatusNotice></div>}

        {loading ? (
          <div className="workbench-panel"><EmptyState loading title="正在读取文档" /></div>
        ) : document ? (
          <>
            {isAdministrator && (
              <SectionPanel className="mb-6 border-accent/30 p-5">
                <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h2 className="font-semibold">管理员编辑</h2>
                    <p className="mt-1 text-[12px] text-muted">使用 Markdown 编写文档；图片可通过按钮上传并插入光标位置。</p>
                  </div>
                  <span className="text-[12px] text-muted">版本 {document.version} · {formatUpdatedAt(document.updated_at)}</span>
                </div>
                <textarea
                  ref={textareaRef}
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  spellCheck={false}
                  aria-label="使用文档 Markdown 内容"
                  className="min-h-[28rem] w-full resize-y rounded-[7px] border border-border-light bg-bg p-4 font-mono text-[13px] leading-6 text-fg outline-none transition-colors focus:border-accent/60 focus:ring-2 focus:ring-accent/15"
                />
                <input
                  ref={fileRef}
                  type="file"
                  accept={ACCEPTED_IMAGE_TYPES}
                  className="hidden"
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (file) void handleImageUpload(file);
                  }}
                />
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    onClick={() => fileRef.current?.click()}
                    disabled={uploading || saving}
                    className="flex items-center gap-1.5 rounded-[7px] border border-border-light px-3 py-2 text-[13px] text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg disabled:opacity-50"
                  >
                    {uploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <ImagePlus className="h-4 w-4" />}
                    {uploading ? "上传中…" : "上传图片并插入"}
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleSave()}
                    disabled={saving || uploading || draft === document.content}
                    className="flex items-center gap-1.5 rounded-[7px] bg-accent px-4 py-2 text-[13px] font-medium text-white transition-colors hover:bg-accent-hover disabled:opacity-50"
                  >
                    {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                    {saving ? "保存中…" : "保存文档"}
                  </button>
                </div>
              </SectionPanel>
            )}

            <div className="flex items-start gap-8">
              <UsageDocOutline items={outline} />
              <div className="min-w-0 flex-1">
                <UsageDocMobileOutline items={outline} />
                <article className="workbench-panel p-5 sm:p-8">
                  <MarkdownContent content={document.content} outline={outline} />
                </article>
              </div>
            </div>
          </>
        ) : null}
      </div>
    </AppFrame>
  );
}
