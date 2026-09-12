"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { BookOpenText, FileText, Loader2, MessageSquare, ExternalLink } from "lucide-react";
import { AppFrame } from "@/components/AppFrame";
import { EmptyState, PageHeader } from "@/components/WorkbenchUI";
import { listMyFiles, formatFileSize, formatDateTime, type UploadedFile } from "@/lib/files-api";
import { openReader, readerHref } from "@/lib/reader-api";

// Reading-studio entry: lists every PDF the current user owns (uploaded in
// ANY chat session) and opens the full-screen workbench for one. Reopening a
// paper resumes its reading session — position, notes and threads persist.
export default function ReaderEntryPage() {
  const router = useRouter();
  const [files, setFiles] = useState<UploadedFile[] | null>(null);
  const [error, setError] = useState("");
  const [openingId, setOpeningId] = useState<string | null>(null);

  useEffect(() => {
    listMyFiles()
      .then((all) => setFiles(all.filter((f) => (f.ext || "").toLowerCase() === "pdf")))
      .catch((e) => {
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

  return (
    <AppFrame>
      <div className="library-canvas flex-1 overflow-y-auto">
        <PageHeader eyebrow="Reading Studio" title="阅研" description="在原文中划选翻译、页内提问、记录发现；阅读进度与笔记按论文保留，随时续读。" icon={<BookOpenText className="h-5 w-5" />} />

        {error && (
          <div className="mb-4 rounded-[7px] border border-error/30 bg-accent2-soft/50 px-3.5 py-2.5 text-[12px] text-error">
            {error}
          </div>
        )}

        {files === null ? (
          <div className="workbench-panel"><EmptyState loading title="正在读取你的论文" /></div>
        ) : files.length === 0 ? (
          <div className="workbench-panel"><EmptyState icon={<FileText className="h-7 w-7" />} title="还没有可阅读的 PDF" description="在对话中上传论文 PDF 后，它会出现在这里。支持划选翻译、页内提问与发现记录。" action={<button onClick={() => router.push("/chat")} className="btn-primary"><MessageSquare className="h-3.5 w-3.5" />去对话上传</button>} /></div>
        ) : (
          <><div className="library-toolbar"><span>我的阅读书架</span><span>{files.length} 篇 PDF · 点击继续阅读</span></div><div className="library-list">
            {files.map((file) => (
              <button
                key={file.id}
                onClick={() => void openWorkbench(file)}
                disabled={openingId === file.id}
                className="library-row group disabled:opacity-60"
              >
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[7px] bg-accent-soft/40 text-accent">
                  {openingId === file.id ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <FileText className="h-4 w-4" />
                  )}
                </div>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[13px] font-medium text-fg">{file.filename || "未命名文档"}</p>
                  <p className="mt-1 text-[12px] text-muted tnum">
                    {formatFileSize(file.size)} · {file.char_count > 0 ? `${file.char_count} 字` : "扫描件"} · 上传于 {formatDateTime(file.created_at)}
                  </p>
                </div>
                <ExternalLink className="h-3.5 w-3.5 shrink-0 text-muted transition-colors group-hover:text-accent" />
              </button>
            ))}
          </div></>
        )}
      </div>
    </AppFrame>
  );
}
