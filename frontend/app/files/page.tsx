"use client";
import { Suspense, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  BookOpenText, Download, ExternalLink, FileText, FileSearch, FolderOpen,
  Loader2, Quote, Search, BookOpen,
} from "lucide-react";
import { AppFrame } from "@/components/AppFrame";
import { EmptyState, PageHeader, PageTabs, StatusNotice } from "@/components/WorkbenchUI";
import {
  fileDownloadName, fileDownloadUrl, formatDateTime, formatFileSize,
  listMyFiles, type UploadedFile,
} from "@/lib/files-api";
import { listMyPapers, type CollectedPaper } from "@/lib/papers-api";
import { fetchFileContent, downloadProtectedFile } from "@/lib/chat-api";
import { openReader, readerHref } from "@/lib/reader-api";

// File center: two addressable tabs.
//  · 下载论文集 — network-paper metadata aggregated from all chat sessions;
//    entries link out to the publisher (network papers have no local full text).
//  · 个人文件 — every file the user uploaded, with original download and
//    (for PDFs) a direct jump into the reading studio.

type FileTab = "papers" | "uploads";

export default function FilesPage() {
  return (
    <Suspense fallback={null}>
      <FilesPageInner />
    </Suspense>
  );
}

function FilesPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const tab: FileTab = searchParams.get("tab") === "uploads" ? "uploads" : "papers";

  const switchTab = (next: FileTab) => {
    router.replace(`/files?tab=${next}`, { scroll: false });
  };

  return (
    <AppFrame>
      <div className="library-canvas flex min-h-0 flex-1 flex-col overflow-hidden">
        <PageHeader eyebrow="Research Library" title="文件中心" description="收藏研究线索，整理手边的原文。论文集提供元数据与来源链接，个人文件保留你上传的原件。" icon={<FolderOpen className="h-5 w-5" />} />

        {/* Reader-style underline tabs */}
        <PageTabs>
          <button
            aria-current={tab === "papers" ? "page" : undefined}
            onClick={() => switchTab("papers")}
            className={`flex items-center gap-2 border-b-2 text-[13px] transition-colors ${
              tab === "papers" ? "border-accent font-semibold text-accent" : "border-transparent text-muted hover:text-fg-secondary"
            }`}
          >
            <BookOpenText className="h-3.5 w-3.5" />
            论文集
          </button>
          <button
            aria-current={tab === "uploads" ? "page" : undefined}
            onClick={() => switchTab("uploads")}
            className={`flex items-center gap-2 border-b-2 text-[13px] transition-colors ${
              tab === "uploads" ? "border-accent font-semibold text-accent" : "border-transparent text-muted hover:text-fg-secondary"
            }`}
          >
            <FileText className="h-3.5 w-3.5" />
            个人文件
          </button>
        </PageTabs>

        <div className="min-h-0 flex-1 overflow-y-auto py-5">
          {tab === "papers" ? <PapersTab /> : <UploadsTab />}
        </div>
      </div>
    </AppFrame>
  );
}

function PapersTab() {
  const [papers, setPapers] = useState<CollectedPaper[] | null>(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");

  useEffect(() => {
    listMyPapers().then(setPapers).catch((e) => {
      setError(e instanceof Error ? e.message : "加载失败");
      setPapers([]);
    });
  }, []);

  const filtered = useMemo(() => {
    if (!papers) return null;
    const q = query.trim().toLowerCase();
    if (!q) return papers;
    return papers.filter((p) =>
      p.title.toLowerCase().includes(q) ||
      (p.authors || []).some((a) => a.toLowerCase().includes(q)) ||
      (p.venue || "").toLowerCase().includes(q) ||
      (p.keywords || []).some((k) => k.toLowerCase().includes(q)));
  }, [papers, query]);

  return (
    <div className="flex flex-col gap-4">
      <div className="relative">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索标题、作者、期刊或关键词"
          className="input-base !pl-8 !text-[13px]"
        />
      </div>

      {error && <StatusNotice tone="error">{error}</StatusNotice>}

      {filtered === null ? (
        <div className="workbench-panel"><EmptyState loading title="正在读取论文集" /></div>
      ) : filtered.length === 0 ? (
        <div className="workbench-panel"><EmptyState icon={<BookOpen className="h-7 w-7" />} title={query ? "没有匹配的论文" : "还没有检索到的论文"} description={!query ? "在对话中进行文献检索后，检出的网络论文（元数据与来源链接）会汇总在这里。" : undefined} /></div>
      ) : (
        <div className="flex flex-col gap-2.5">
          <p className="text-[12px] text-muted">共 {filtered.length} 篇 · 网络论文仅提供元数据，全文阅读请上传 PDF</p>
          {filtered.map((paper, index) => (
            <PaperCard key={paper.id || `${paper.title}-${index}`} paper={paper} />
          ))}
        </div>
      )}
    </div>
  );
}

function PaperCard({ paper }: { paper: CollectedPaper }) {
  const [expanded, setExpanded] = useState(false);
  const link = (paper.urls || [])[0] || (paper.doi ? `https://doi.org/${paper.doi}` : "");
  return (
    <article className="rounded-[9px] border border-border-light bg-surface p-4">
      <div className="flex items-start justify-between gap-3">
        <h3 className="min-w-0 text-[13px] font-semibold leading-relaxed text-fg">{paper.title}</h3>
        {paper.in_review && <span className="badge badge-accent shrink-0">已入综述</span>}
      </div>
      <p className="mt-1.5 truncate text-[12px] text-muted">
        {[
          (paper.authors || []).slice(0, 4).join(", ") + ((paper.authors || []).length > 4 ? " 等" : ""),
          paper.year,
          paper.venue,
          paper.source,
          paper.citation_count != null ? `引用 ${paper.citation_count}` : "",
        ].filter(Boolean).join(" · ")}
      </p>
      {paper.abstract && (
        <>
          <p className={`mt-2.5 font-serif text-[12px] leading-[1.85] text-muted ${expanded ? "" : "line-clamp-2"}`}>
            {paper.abstract}
          </p>
          <button onClick={() => setExpanded(!expanded)} className="mt-1.5 text-[12px] text-accent/80 hover:text-accent">
            {expanded ? "收起摘要" : "展开摘要"}
          </button>
        </>
      )}
      <div className="mt-3 flex items-center justify-between border-t border-border-light pt-2.5">
        <span className="flex items-center gap-1.5 text-[12px] text-muted/80">
          <Quote className="h-3 w-3" />
          见于 {paper.sessions?.[0]?.title || "对话"}
          {(paper.sessions?.length || 0) > 1 ? ` 等 ${paper.sessions.length} 个会话` : ""}
        </span>
        {link && (
          <a
            href={link}
            target="_blank"
            rel="noreferrer"
            className="flex items-center gap-1.5 rounded-[6px] px-2 py-1 text-[12px] text-accent transition-colors hover:bg-surface-hover"
          >
            <ExternalLink className="h-3 w-3" />
            查看来源
          </a>
        )}
      </div>
    </article>
  );
}

function UploadsTab() {
  const [files, setFiles] = useState<UploadedFile[] | null>(null);
  const [error, setError] = useState("");
  const [previewId, setPreviewId] = useState<string | null>(null);
  const [openingId, setOpeningId] = useState<string | null>(null);

  useEffect(() => {
    listMyFiles().then(setFiles).catch((e) => {
      setError(e instanceof Error ? e.message : "加载失败");
      setFiles([]);
    });
  }, []);

  const openWorkbench = async (file: UploadedFile) => {
    if (openingId) return;
    setOpeningId(file.id);
    setError("");
    try {
      const opened = await openReader(file.id, null);
      window.location.href = readerHref(opened.session_id, file.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "打开失败，请重试");
      setOpeningId(null);
    }
  };

  const download = (file: UploadedFile) => {
    void downloadProtectedFile(fileDownloadUrl(file.id), fileDownloadName(file));
  };

  return (
    <div className="flex flex-col gap-2.5">
      {error && <StatusNotice tone="error">{error}</StatusNotice>}

      {files === null ? (
        <div className="workbench-panel"><EmptyState loading title="正在读取个人文件" /></div>
      ) : files.length === 0 ? (
        <div className="workbench-panel"><EmptyState icon={<FileText className="h-7 w-7" />} title="还没有上传的文件" description="在对话中上传的 PDF、DOCX、图片等文件会集中保存在这里，随时下载原件或进入阅研。" /></div>
      ) : files.map((file) => (
        <div key={file.id}>
          <div className="upload-row flex items-center gap-3.5 rounded-[9px] border border-border-light bg-surface px-4 py-3">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[7px] bg-accent-soft/40 text-accent">
              <FileText className="h-4 w-4" />
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate text-[13px] font-medium text-fg">{file.filename || "未命名文件"}</p>
              <p className="mt-1 text-[12px] text-muted tnum">
                {(file.ext || "?").toUpperCase()} · {formatFileSize(file.size)} · {file.char_count > 0 ? `${file.char_count} 字` : "无文本"} · {formatDateTime(file.created_at)}
              </p>
            </div>
            <div className="upload-actions flex shrink-0 items-center gap-1">
              {file.char_count > 0 && (
                <button
                  onClick={() => setPreviewId(previewId === file.id ? null : file.id)}
                  title="预览提取文本"
                  className="btn-ghost h-7 w-7"
                >
                  <FileSearch className="h-3.5 w-3.5" />
                </button>
              )}
              <button onClick={() => download(file)} title="下载原件" className="btn-ghost h-7 w-7">
                <Download className="h-3.5 w-3.5" />
              </button>
              {(file.ext || "").toLowerCase() === "pdf" && (
                <button
                  onClick={() => void openWorkbench(file)}
                  disabled={openingId === file.id}
                  title="在阅研中打开"
                  className="btn-primary !px-2.5 !py-1.5"
                >
                  {openingId === file.id ? <Loader2 className="h-3 w-3 animate-spin" /> : <BookOpen className="h-3 w-3" />}
                  阅研
                </button>
              )}
            </div>
          </div>
          {previewId === file.id && <TextPreview id={file.id} />}
        </div>
      ))}
    </div>
  );
}

function TextPreview({ id }: { id: string }) {
  const [state, setState] = useState<{ text: string; truncated: boolean } | "loading" | "error">("loading");

  useEffect(() => {
    setState("loading");
    fetchFileContent(id)
      .then((data) => setState({ text: data.text, truncated: Boolean(data.truncated) }))
      .catch(() => setState("error"));
  }, [id]);

  return (
    <div className="mt-1.5 max-h-64 overflow-y-auto rounded-[7px] border border-border-light bg-surface-sunken/60 px-3.5 py-3">
      {state === "loading" ? (
        <p className="text-[12px] text-muted">加载中…</p>
      ) : state === "error" ? (
        <p className="text-[12px] text-error">无法读取提取文本</p>
      ) : (
        <pre className="whitespace-pre-wrap font-mono text-[12px] leading-relaxed text-fg-secondary">
          {state.text}
          {state.truncated ? "\n…（内容过长已截断）" : ""}
        </pre>
      )}
    </div>
  );
}
