"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ArrowLeft, BookOpenText, ImagePlus, Loader2, Save } from "lucide-react";
import { useRouter } from "next/navigation";
import { Nav } from "@/components/Nav";
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

function MarkdownContent({ content }: { content: string }) {
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
        components={{
          a: ({ node: _node, ...props }) => (
            <a {...props} target="_blank" rel="noopener noreferrer" />
          ),
          img: ({ node: _node, ...props }) => (
            // eslint-disable-next-line @next/next/no-img-element
            <img {...props} loading="lazy" alt={props.alt || "文档图片"} />
          ),
        }}
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
    <div className="min-h-screen bg-bg text-fg">
      <Nav />
      <main className="mx-auto max-w-5xl px-4 py-8 sm:px-8">
        <div className="mb-6 flex items-start gap-3">
          <button
            type="button"
            onClick={() => router.push("/chat")}
            aria-label="返回对话"
            title="返回对话"
            className="mt-1 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border-light text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg"
          >
            <ArrowLeft className="h-4 w-4" />
          </button>
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-accent-soft/50 text-accent">
            <BookOpenText className="h-5 w-5" />
          </div>
          <div className="min-w-0">
            <h1 className="text-2xl font-bold">使用文档</h1>
            <p className="mt-1 text-sm text-muted">智能体功能介绍、使用方法与注意事项</p>
          </div>
        </div>

        {error && <div className="mb-4 rounded-lg border border-error/40 bg-error/10 p-3 text-sm text-error">{error}</div>}
        {notice && <div className="mb-4 rounded-lg border border-success/40 bg-success/10 p-3 text-sm text-success">{notice}</div>}

        {loading ? (
          <div className="flex min-h-48 items-center justify-center rounded-2xl border border-border-light bg-surface">
            <Loader2 className="h-6 w-6 animate-spin text-accent" />
          </div>
        ) : document ? (
          <>
            {isAdministrator && (
              <section className="mb-6 rounded-2xl border border-accent/30 bg-surface p-5 shadow-sm">
                <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h2 className="font-semibold">管理员编辑</h2>
                    <p className="mt-1 text-xs text-muted">使用 Markdown 编写文档；图片可通过按钮上传并插入光标位置。</p>
                  </div>
                  <span className="text-xs text-muted">版本 {document.version} · {formatUpdatedAt(document.updated_at)}</span>
                </div>
                <textarea
                  ref={textareaRef}
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  spellCheck={false}
                  aria-label="使用文档 Markdown 内容"
                  className="min-h-[28rem] w-full resize-y rounded-xl border border-border-light bg-bg p-4 font-mono text-sm leading-6 text-fg outline-none transition-colors focus:border-accent/60 focus:ring-2 focus:ring-accent/15"
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
                    className="flex items-center gap-1.5 rounded-lg border border-border-light px-3 py-2 text-sm text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg disabled:opacity-50"
                  >
                    {uploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <ImagePlus className="h-4 w-4" />}
                    {uploading ? "上传中…" : "上传图片并插入"}
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleSave()}
                    disabled={saving || uploading || draft === document.content}
                    className="flex items-center gap-1.5 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-accent-hover disabled:opacity-50"
                  >
                    {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                    {saving ? "保存中…" : "保存文档"}
                  </button>
                </div>
              </section>
            )}

            <article className="rounded-2xl border border-border-light bg-surface p-5 shadow-sm sm:p-8">
              <div className="mb-6 border-b border-border-light pb-4">
                <h2 className="text-xl font-semibold">{document.title}</h2>
                <p className="mt-1 text-xs text-muted">最后更新：{formatUpdatedAt(document.updated_at)}</p>
              </div>
              <MarkdownContent content={document.content} />
            </article>
          </>
        ) : null}
      </main>
    </div>
  );
}
