"use client";
import { useState, useEffect, memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Brain, ChevronDown, ChevronRight, Loader2, Sparkles, Search, Map, BookOpen, FileText, Download, Copy, Check, RefreshCw, Route, ExternalLink, Quote, ClipboardCheck, FileCheck, FileDown, ShieldCheck, FileInput, Images, Image as ImageIcon, BarChart3, ZoomIn } from "lucide-react";
import { MessageSquareQuote } from "lucide-react";
import { useUIStore } from "@/stores/ui";
import { useChatStore } from "@/stores/chat";
import { GenealogyGraph } from "@/components/GenealogyGraph";
import type { Paper, CandidatePaper, ResearchMapData, ReadingPathStep } from "@/lib/types";
import { attachmentMetaLabel, downloadProtectedFile, fetchFileBlob, isImageAttachment, type ChatAttachment } from "@/lib/chat-api";

export interface ChatMsg {
  role: "user" | "assistant";
  content: string;
  thinking?: string;
  toolCalls?: ToolCall[];
  isStreaming?: boolean;
  attachments?: ChatAttachment[];
}
interface ToolCall { name: string; result?: unknown }

interface ToolResultShape {
  status?: string;
  summary?: string;
  error?: { code?: string; message?: string };
}

/* ---------- shared bits ---------- */

const TOOL_META: Record<string, { label: string; icon: typeof Search }> = {
  search_papers: { label: "文献检索", icon: Search },
  deep_read: { label: "深度阅读", icon: BookOpen },
  ask_papers: { label: "论文问答", icon: MessageSquareQuote },
  research_map: { label: "研究地图", icon: Map },
  reading_path: { label: "阅读路径", icon: Route },
  write_review: { label: "文献综述", icon: FileText },
  citation_export: { label: "参考文献导出", icon: Quote },
  export_report: { label: "报告导出", icon: Download },
  check_structure: { label: "结构体检", icon: ClipboardCheck },
  check_format: { label: "格式检查", icon: FileCheck },
  export_manuscript: { label: "文稿导出", icon: FileDown },
  integrity_sweep: { label: "可靠性质检", icon: ShieldCheck },
  bib_import: { label: "文献库导入", icon: FileInput },
  exhibit_index: { label: "图表导览", icon: Images },
  explain_element: { label: "元素解读", icon: ZoomIn },
  field_census: { label: "领域普查", icon: BarChart3 },
};

// Legacy tool names in pre-refactor histories.
const LEGACY_TOOL_NAMES: Record<string, string> = {
  search: "search_papers",
  review: "write_review",
  export_bibtex: "citation_export",
};

type ToolStatus = "running" | "success" | "partial" | "error";

function statusOf(result: unknown): ToolStatus {
  const s = (result as ToolResultShape | undefined)?.status;
  if (!result) return "running";
  if (s === "error") return "error";
  if (s === "partial") return "partial";
  return "success";
}

function StatusDot({ status }: { status: ToolStatus }) {
  if (status === "running") {
    return (
      <span className="relative flex h-2 w-2">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-60" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-accent" />
      </span>
    );
  }
  const color = status === "success" ? "bg-success" : status === "partial" ? "bg-warning" : "bg-error";
  return <span className={`inline-flex h-2 w-2 rounded-full ${color}`} />;
}

function CardHeader({ name, result, expanded, onToggle, meta_text }: {
  name: string; result: unknown; expanded: boolean; onToggle: () => void; meta_text?: string;
}) {
  const meta = TOOL_META[name] || { label: name, icon: Sparkles };
  const Icon = meta.icon;
  const r = (result || {}) as ToolResultShape;
  return (
    <button
      onClick={onToggle}
      className="flex w-full items-center gap-2.5 rounded-xl px-3 py-2 text-left transition-colors hover:bg-surface-hover/60"
    >
      <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-accent-soft/60">
        <Icon className="h-3.5 w-3.5 text-accent" />
      </div>
      <span className="text-[13px] font-medium text-fg-secondary">{meta.label}</span>
      <StatusDot status={statusOf(result)} />
      {meta_text && <span className="truncate text-xs text-muted">{meta_text}</span>}
      {r.error && <span className="truncate text-xs text-error/70">{r.error.message}</span>}
      <ChevronRight className={`ml-auto h-3.5 w-3.5 shrink-0 text-muted/50 transition-transform ${expanded ? "rotate-90" : ""}`} />
    </button>
  );
}

function AssistantActions({ msg, isLast, disabled, onRegenerate }: {
  msg: ChatMsg; isLast?: boolean; disabled?: boolean; onRegenerate?: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(msg.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch { /* clipboard unavailable */ }
  };
  return (
    <div className="mt-1 flex items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100">
      <button onClick={copy}
        className="flex items-center gap-1 rounded-md px-2 py-1 text-[11px] text-muted transition-colors hover:bg-surface-hover hover:text-fg"
        title="复制 Markdown"
      >
        {copied ? <Check className="h-3 w-3 text-success" /> : <Copy className="h-3 w-3" />}
        {copied ? "已复制" : "复制"}
      </button>
      {isLast && onRegenerate && (
        <button onClick={onRegenerate} disabled={disabled}
          className="flex items-center gap-1 rounded-md px-2 py-1 text-[11px] text-muted transition-colors hover:bg-surface-hover hover:text-fg disabled:opacity-40"
          title="重新生成回答"
        >
          <RefreshCw className="h-3 w-3" />
          重新生成
        </button>
      )}
    </div>
  );
}

function FileChip({ attach }: { attach: ChatAttachment }) {
  const openFile = useUIStore(s => s.openFile);
  const image = isImageAttachment(attach);
  return (
    <button
      onClick={() => openFile(attach)}
      className="flex max-w-[300px] items-center gap-1.5 rounded-lg border border-border-light bg-surface px-2 py-1 text-[11px] text-fg-secondary transition-colors hover:border-accent/30 hover:bg-surface-hover/60 hover:text-fg"
      title={`${attach.filename} — 点击${image ? "预览图片" : "查看提取文本"}`}
    >
      {image ? <ImageIcon className="h-3 w-3 shrink-0 text-accent" /> : <FileText className="h-3 w-3 shrink-0 text-accent" />}
      <span className="max-w-[180px] truncate">{attach.filename}</span>
      <span className="tnum whitespace-nowrap text-muted/60">{attachmentMetaLabel(attach)}</span>
    </button>
  );
}

function paperUrl(p: { urls?: Record<string, string>; doi?: string | null }): string {
  if (p.urls) {
    for (const k of ["doi", "openalex", "arxiv", "crossref", "europepmc", "doaj"]) {
      const u = p.urls[k];
      if (u && u.startsWith("http")) return u;
    }
    const first = Object.values(p.urls)[0];
    if (first && first.startsWith("http")) return first;
  }
  if (p.doi) return `https://doi.org/${p.doi.replace(/^\//, "")}`;
  return "";
}


/* ---------- search_papers ---------- */

function SearchResultCard({ result }: { result: Record<string, unknown> }) {
  const [expanded, setExpanded] = useState(true);
  const [showCandidates, setShowCandidates] = useState(false);
  const papers = (result.papers || []) as Paper[];
  const candidates = (result.candidates || []) as CandidatePaper[];
  const subdirs = (result.sub_directions || []) as { name: string }[];
  const sourceNotices = (result.source_notices || []) as string[];
  const isError = Boolean(result.error);

  return (
    <div className="my-2">
      <CardHeader name="search_papers" result={result} expanded={expanded}
        onToggle={() => setExpanded(!expanded)}
        meta_text={isError ? undefined : `核心集 ${papers.length} 篇 · 候选 ${candidates.length} 篇 · 仅展示元数据与有效摘要`} />
      {expanded && !isError && (
        <div className="mt-1.5 space-y-2 pl-1">
          {sourceNotices.map((notice, index) => (
            <div key={`source-notice-${index}`} className="rounded-lg border border-warning/25 bg-warning/5 px-2.5 py-2 text-[11px] text-fg-secondary">
              {notice}
            </div>
          ))}
          {subdirs.length > 0 && (
            <div className="flex flex-wrap gap-1.5 px-1">
              {subdirs.map((d, i) => (
                <span key={i} className="badge badge-muted">{d.name}</span>
              ))}
            </div>
          )}
          <div className="space-y-1.5">
            {papers.map((p, i) => (
              <div key={p.id || i} className="flex items-start gap-2.5 rounded-xl border border-border-light bg-surface px-3 py-2">
                <span className="tnum mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-accent-soft/60 text-[10px] font-bold text-accent">
                  {i + 1}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-[13px] font-medium leading-snug text-fg">{p.title}</p>
                  <p className="mt-0.5 text-[11px] text-muted">
                    {p.year || "n.d."} · 被引 <span className="tnum">{p.citation_count}</span>
                    {p.relevance_score >= 0 && <> · 相关度 <span className="tnum">{p.relevance_score.toFixed(2)}</span></>}
                  </p>
                </div>
                <div className="mt-0.5 flex shrink-0 items-center gap-1.5">
                  {paperUrl(p) && (
                    <a href={paperUrl(p)} target="_blank" rel="noreferrer"
                      className="text-muted/50 transition-colors hover:text-accent" title="打开原文">
                      <ExternalLink className="h-3.5 w-3.5" />
                    </a>
                  )}
                </div>
              </div>
            ))}
          </div>
          {candidates.length > 0 && (
            <div>
              <button onClick={() => setShowCandidates(!showCandidates)}
                className="flex items-center gap-1 px-1 py-1 text-[11px] text-muted transition-colors hover:text-fg-secondary">
                <ChevronRight className={`h-3 w-3 transition-transform ${showCandidates ? "rotate-90" : ""}`} />
                候选论文（{candidates.length}）
              </button>
              {showCandidates && (
                <ul className="mt-1 space-y-1 px-1">
                  {candidates.map((c, i) => (
                    <li key={c.id || i} className="flex items-center gap-1.5 text-[12px] text-fg-secondary">
                      <span className="text-muted">- </span>
                      <span className="min-w-0 truncate">{c.title}</span>
                      <span className="text-muted/60 tnum shrink-0">{c.year || ""}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ---------- research_map ---------- */

function ResearchMapCard({ result }: { result: Record<string, unknown> }) {
  const [expanded, setExpanded] = useState(true);
  const clusters = (result.clusters || []) as ResearchMapData["clusters"];
  const timeline = (result.timeline || []) as ResearchMapData["timeline"];
  const landscape = (result.landscape || "") as string;
  const graph = (result.graph || { nodes: [], edges: [] }) as ResearchMapData["graph"];
  const isError = Boolean(result.error);
  const clusterLabels = Object.fromEntries(clusters.map(c => [c.id, c.label]));

  return (
    <div className="my-2">
      <CardHeader name="research_map" result={result} expanded={expanded}
        onToggle={() => setExpanded(!expanded)}
        meta_text={isError ? undefined : `${clusters.length} 个主题簇 · ${graph.nodes.length} 节点`} />
      {expanded && !isError && (
        <div className="mt-1.5 space-y-3 pl-1">
          {landscape && (
            <div className="rounded-xl border border-border-light bg-surface p-3.5">
              <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-accent">领域脉络</div>
              <p className="text-[13px] leading-relaxed text-fg-secondary">{landscape}</p>
            </div>
          )}
          {graph.nodes.length > 0 && (
            <GenealogyGraph data={graph} clusterLabels={clusterLabels} />
          )}
          {clusters.length > 0 && (
            <div className="space-y-2">
              {clusters.map((c) => (
                <details key={c.id} className="rounded-xl border border-border-light bg-surface">
                  <summary className="flex cursor-pointer items-center gap-2 p-3">
                    <span className="tnum flex h-5 w-5 items-center justify-center rounded-md bg-accent-soft/60 text-[10px] font-bold text-accent">{c.id + 1}</span>
                    <span className="text-sm font-semibold">{c.label}</span>
                    <span className="rounded-full bg-surface-hover px-2 py-0.5 text-[10px] text-muted tnum">{c.papers.length} 篇</span>
                  </summary>
                  <div className="border-t border-border-light p-3">
                    {c.overview && <p className="mb-2 text-sm italic leading-relaxed text-muted">{c.overview}</p>}
                    <ul className="space-y-1 text-sm text-fg-secondary">
                      {c.papers.map(p => (
                        <li key={p.id} className="flex gap-2">
                          <span className="text-muted">-</span>
                          <span>{p.title}<span className="ml-1 text-[11px] text-muted/60 tnum">{p.year || ""}</span></span>
                        </li>
                      ))}
                    </ul>
                  </div>
                </details>
              ))}
            </div>
          )}
          {timeline.length > 0 && (
            <div className="rounded-xl border border-border-light bg-surface p-3.5">
              <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-accent">时间脉络</div>
              <div className="space-y-1.5">
                {timeline.map(t => (
                  <div key={t.year} className="flex items-start gap-2.5 text-[12px]">
                    <span className="tnum w-10 shrink-0 font-semibold text-accent">{t.year}</span>
                    <span className="text-fg-secondary">
                      {t.papers.slice(0, 3).map(p => p.title).join("；")}
                      {t.papers.length > 3 && ` 等 ${t.papers.length} 篇`}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ---------- reading_path ---------- */

const ROLE_BADGE: Record<string, string> = {
  "奠基": "badge-accent2",
  "桥梁": "badge-accent",
  "前沿": "badge-success",
};

function ReadingPathCard({ result }: { result: Record<string, unknown> }) {
  const [expanded, setExpanded] = useState(true);
  const path = (result.path || []) as ReadingPathStep[];
  const isError = Boolean(result.error);

  return (
    <div className="my-2">
      <CardHeader name="reading_path" result={result} expanded={expanded}
        onToggle={() => setExpanded(!expanded)}
        meta_text={isError ? undefined : `${path.length} 篇推荐阅读`} />
      {expanded && !isError && path.length > 0 && (
        <ol className="mt-1.5 space-y-2 pl-1">
          {path.map((p, i) => (
            <li key={p.paper_id || i} className="flex items-start gap-2.5 rounded-xl border border-border-light bg-surface px-3 py-2.5">
              <span className="tnum mt-0.5 text-[15px] font-bold text-accent/70">{i + 1}</span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <span className={`badge ${ROLE_BADGE[p.role] || "badge-muted"}`}>{p.role}</span>
                  <p className="text-[13px] font-medium leading-snug text-fg">{p.title}</p>
                </div>
                {p.reason && <p className="mt-1 text-[12px] leading-relaxed text-muted">{p.reason}</p>}
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

/* ---------- write_review ---------- */

function ReviewCard({ result }: { result: Record<string, unknown> }) {
  const [expanded, setExpanded] = useState(true);
  const review = (result.literature_review || "") as string;
  const isError = Boolean(result.error);

  const files = (result.files || []) as { fileName: string; url?: string; displayName?: string }[];

  return (
    <div className="my-2">
      <CardHeader name="write_review" result={result} expanded={expanded}
        onToggle={() => setExpanded(!expanded)}
        meta_text={isError ? undefined : `${review.length} 字`} />
      {expanded && !isError && review && (
        <div className="mt-1.5 space-y-2 pl-1">
          <div className="flex justify-end">
              {files.map((file) => (
                <button key={file.fileName} onClick={() => void downloadProtectedFile(file.url || `/files/${file.fileName}`, file.fileName)}
                  className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium text-muted transition-colors hover:bg-surface-hover hover:text-fg"
                  title={`下载 ${file.displayName || file.fileName}`}>
                  <Download className="h-3.5 w-3.5" />
                  {file.displayName || file.fileName}
                </button>
              ))}
          </div>
          <div className="max-h-[520px] overflow-y-auto rounded-xl border border-border-light bg-surface p-4">
            <div className="chat-prose text-sm leading-relaxed text-fg-secondary">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{review}</ReactMarkdown>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* ---------- deep_read ---------- */

const SUMMARY_FIELDS: { key: string; label: { en: string; zh: string } }[] = [
  { key: "research_problem", label: { en: "Problem", zh: "研究问题" } },
  { key: "methodology", label: { en: "Methodology", zh: "方法" } },
  { key: "theoretical_framework", label: { en: "Framework", zh: "理论框架" } },
  { key: "key_findings", label: { en: "Key findings", zh: "主要发现" } },
  { key: "contributions", label: { en: "Contributions", zh: "贡献" } },
  { key: "limitations", label: { en: "Limitations", zh: "局限" } },
  { key: "datasets", label: { en: "Datasets", zh: "数据集" } },
  { key: "baselines", label: { en: "Baselines", zh: "基线" } },
  { key: "sample_size", label: { en: "Sample size", zh: "样本量" } },
  { key: "interventions", label: { en: "Interventions", zh: "干预" } },
  { key: "outcomes", label: { en: "Outcomes", zh: "结果" } },
  { key: "future_work", label: { en: "Future work", zh: "未来工作" } },
];

interface SectionOutlineEntry { title: string; page_start?: number; page_end?: number }
interface DocumentInfo {
  read_level?: string; document_ready?: boolean; parse_status?: string; parser_backend?: string;
  page_count?: number; text_chars?: number; section_count?: number;
  is_scanned?: boolean; ocr_status?: string; ocr_chars?: number;
  element_count?: number; vision_understood_count?: number;
}
interface DeepReadSummary {
  paper_id?: string; title?: string; section_outline?: SectionOutlineEntry[];
  document_info?: DocumentInfo; [key: string]: unknown;
}

function _summaryEntries(s: DeepReadSummary, lang: "zh" | "en"): [string, string | string[]][] {
  const out: [string, string | string[]][] = [];
  for (const f of SUMMARY_FIELDS) {
    const v = s[f.key];
    if (v == null) continue;
    if (Array.isArray(v)) { if (v.length) out.push([f.label[lang], v as string[]]); }
    else if (typeof v === "string" && v.trim()) out.push([f.label[lang], v]);
  }
  return out;
}

interface DeepReadAttachmentResult {
  id: string;
  filename?: string;
  status?: string;
  element_count?: number;
  char_count?: number;
  error?: string;
}

function DeepReadCard({ result }: { result: Record<string, unknown> }) {
  const { uiLang: lang } = useChatStore();
  const summaries = Object.entries((result.summaries || {}) as Record<string, DeepReadSummary>);
  const attachments = (result.attachments || []) as DeepReadAttachmentResult[];
  const failures = (result.failures || []) as { paper_id: string; title: string; reason: string }[];
  const isError = Boolean(result.error);
  const [expanded, setExpanded] = useState(summaries.length + attachments.length <= 3);
  const metaParts = [
    summaries.length
      ? `${summaries.length} 个上传文档摘要`
      : "",
    attachments.length ? `${attachments.length} 个附件` : "",
    failures.length ? `失败 ${failures.length}` : "",
  ].filter(Boolean);

  return (
    <div className="my-2">
      <CardHeader name="deep_read" result={result} expanded={expanded}
        onToggle={() => setExpanded(!expanded)}
        meta_text={isError ? undefined : (metaParts.join(" · ") || "已完成")} />
      {expanded && !isError && (
        <div className="mt-1.5 max-h-[560px] space-y-2 overflow-y-auto pl-1">
          {attachments.length > 0 && (
            <div className="rounded-xl border border-border-light bg-surface p-3">
              <div className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted">
                {lang === "zh" ? "上传附件" : "Uploaded attachments"}
              </div>
              <div className="space-y-1.5">
                {attachments.map((a) => {
                  const ready = a.status === "ready";
                  const degraded = a.status === "degraded" || a.status === "legacy_text_only";
                  const statusText = ready
                    ? (lang === "zh" ? "已完成多模态理解" : "Multimodal understanding ready")
                    : degraded
                      ? (lang === "zh" ? "已降级为文本理解" : "Degraded to text understanding")
                      : (a.status || (lang === "zh" ? "已处理" : "Processed"));
                  return (
                    <div key={a.id} className="flex items-start gap-2 text-xs text-fg-secondary">
                      <FileCheck className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${ready ? "text-success" : degraded ? "text-warning" : "text-accent"}`} />
                      <div className="min-w-0 flex-1">
                        {a.filename && <div className="truncate font-medium text-fg" title={a.filename}>{a.filename}</div>}
                        <span>{statusText}</span>
                        {(a.element_count || 0) > 0 && <span className="ml-1.5 text-muted">· {a.element_count} {lang === "zh" ? "个图表/公式元素" : "visual elements"}</span>}
                        {a.error && <div className="mt-0.5 break-words text-[11px] text-error/70">{a.error}</div>}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
          {summaries.map(([pid, s]) => (
            <DeepReadPaperEntry key={pid} pid={pid} summary={s} lang={lang} />
          ))}
          {failures.length > 0 && (
            <div className="rounded-xl border border-error/20 bg-error/5 p-3">
              <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-error/80">
                {lang === "zh" ? "提取失败" : "Extraction failed"}
              </div>
              {failures.map((f, i) => (
                <div key={i} className="text-xs text-fg-secondary">
                  <span className="font-medium">{f.title}</span>
                  <span className="ml-1.5 text-error/70">({f.reason})</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function DeepReadPaperEntry({ pid, summary, lang }: { pid: string; summary: DeepReadSummary; lang: "zh" | "en" }) {
  const setComposerDraft = useUIStore((st) => st.setComposerDraft);
  const [open, setOpen] = useState(false);
  const fields = _summaryEntries(summary, lang);
  const elements = (summary.elements || []) as { element_id: string; kind: string; page?: number; caption?: string }[];
  const outline = (summary.section_outline || []) as SectionOutlineEntry[];
  const info = (summary.document_info || {}) as DocumentInfo;
  const title = summary.title || pid;
  const fieldCount = fields.length;
  const elemCount = elements.length;
  const levelLabel = lang === "zh" ? "上传全文级" : "uploaded full text";
  const fieldLabel = fieldCount
    ? `${fieldCount} ${lang === "zh" ? "字段" : "fields"}`
    : (lang === "zh" ? "无字段" : "no fields");
  const preview = `${levelLabel} · ${fieldLabel}`;
  const ocrLabel = info.ocr_status === "not_needed"
    ? (lang === "zh" ? "数字文本，无需 VLM-OCR" : "Digital text; VLM-OCR not needed")
    : info.ocr_status === "recovered"
      ? (lang === "zh" ? `扫描 OCR 已恢复 ${info.ocr_chars || 0} 字符` : `Scan OCR recovered ${info.ocr_chars || 0} chars`)
      : info.ocr_status === "unavailable_or_empty"
        ? (lang === "zh" ? "扫描 OCR 未恢复，已降级" : "Scan OCR unavailable; degraded")
        : (lang === "zh" ? "OCR 未尝试" : "OCR not attempted");
  return (
    <div className="rounded-xl border border-border-light bg-surface">
      <button onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-surface-hover/60">
        <BookOpen className="h-3.5 w-3.5 shrink-0 text-accent" />
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-fg">{title}</span>
        <span className="shrink-0 text-[11px] text-success">
          {preview}
        </span>
        {elemCount > 0 && (
          <span className="shrink-0 rounded bg-accent-soft/50 px-1.5 py-0.5 text-[10px] font-medium text-accent">
            {elemCount} {lang === "zh" ? "图表" : "exhibits"}
          </span>
        )}
        <ChevronRight className={`h-3.5 w-3.5 shrink-0 text-muted/50 transition-transform ${open ? "rotate-90" : ""}`} />
      </button>
      {open && (fields.length > 0 || elemCount > 0 || outline.length > 0 || Object.keys(info).length > 0) && (
        <div className="space-y-2 border-t border-border-light p-3">
          {Object.keys(info).length > 0 && (
            <div className="rounded-lg bg-surface-hover/40 p-2 text-[11px] text-muted">
              <div className="flex flex-wrap gap-x-3 gap-y-1">
                <span>{info.document_ready ? (lang === "zh" ? "文档已解析" : "Document parsed") : (lang === "zh" ? "尚未完成解析" : "Not fully parsed")}</span>
                {info.parse_status && <span>{lang === "zh" ? "解析状态" : "Parse"}: {info.parse_status}</span>}
                {info.parser_backend && <span>{lang === "zh" ? "解析器" : "Parser"}: {info.parser_backend}</span>}
                {(info.page_count || 0) > 0 && <span>{info.page_count} {lang === "zh" ? "页" : "pages"}</span>}
                {(info.text_chars || 0) > 0 && <span>{Number(info.text_chars).toLocaleString()} {lang === "zh" ? "文本字符" : "text chars"}</span>}
                <span>{info.section_count || 0} {lang === "zh" ? "章节" : "sections"}</span>
                <span>{ocrLabel}</span>
                {(info.vision_understood_count || 0) > 0 && <span>{info.vision_understood_count}/{info.element_count || elemCount} {lang === "zh" ? "元素已视觉理解" : "visual elements understood"}</span>}
              </div>
            </div>
          )}
          {outline.length > 0 && (
            <details className="rounded-lg border border-border-light bg-surface-hover/20 p-2">
              <summary className="cursor-pointer text-[11px] font-medium text-fg-secondary">
                {lang === "zh" ? `完整章节目录（${outline.length}）` : `Section outline (${outline.length})`}
              </summary>
              <ol className="mt-1.5 space-y-0.5 text-[11px] text-muted">
                {outline.map((sec, i) => {
                  const start = sec.page_start || 0;
                  const end = sec.page_end || start;
                  const pages = start ? (end && end !== start ? `p.${start}-${end}` : `p.${start}`) : "";
                  return <li key={`${sec.title}-${i}`} className="flex gap-2"><span className="min-w-0 flex-1">{sec.title}</span><span className="shrink-0 text-muted/60">{pages}</span></li>;
                })}
              </ol>
            </details>
          )}
          {fields.map(([label, value], i) => (
            <div key={i} className="text-sm text-fg-secondary">
              <span className="font-medium">{label}:</span>{" "}
              {Array.isArray(value) ? (
                <ul className="ml-4 mt-1 list-disc">
                  {value.map((f, j) => <li key={j}>{f}</li>)}
                </ul>
              ) : (<span>{String(value)}</span>)}
            </div>
          ))}
          {elemCount > 0 && (
            <div className="pt-1">
              <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted">
                {lang === "zh" ? "图表元素（点击深入解读）" : "Exhibits (click to explain)"}
              </div>
              <div className="flex flex-wrap gap-1.5">
                {elements.map((el) => {
                  const isTable = el.kind === "table";
                  const isFormula = el.kind === "formula";
                  const badge = isTable ? "表" : isFormula ? "式" : "图";
                  const cap = (el.caption || "").slice(0, 60);
                  return (
                    <button key={el.element_id}
                      onClick={() => setComposerDraft(`请详细解读 ${el.element_id}`)}
                      title={el.caption || el.element_id}
                      className="flex max-w-full items-center gap-1 rounded-lg border border-border-light bg-surface-hover/40 px-1.5 py-1 text-left text-[11px] transition-colors hover:bg-surface-hover/80">
                      <span className={`shrink-0 rounded px-1 py-0.5 text-[9px] font-medium ${
                        isTable ? "bg-accent-soft/60 text-accent"
                        : isFormula ? "bg-success/15 text-success"
                        : "bg-warning/15 text-warning"}`}>
                        {badge}
                      </span>
                      {el.page ? <span className="shrink-0 text-muted/50">p.{el.page}</span> : null}
                      <span className="truncate text-muted">{cap || el.element_id}</span>
                    </button>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ---------- ask_papers ---------- */

interface AskPapersSource { paper_id: string; title?: string; sections?: string[]; snippet?: string }

function AnswerCard({ result }: { result: Record<string, unknown> }) {
  const [expanded, setExpanded] = useState(true);
  const answer = (result.answer || "") as string;
  const sources = (result.sources || []) as AskPapersSource[];
  const isError = Boolean(result.error);

  return (
    <div className="my-2">
      <CardHeader name="ask_papers" result={result} expanded={expanded}
        onToggle={() => setExpanded(!expanded)}
        meta_text={isError ? undefined : `${answer.length} 字 · ${sources.length} 个来源`} />
      {expanded && !isError && answer && (
        <div className="mt-1.5 space-y-2 pl-1">
          <div className="max-h-[480px] overflow-y-auto rounded-xl border border-border-light bg-surface p-4">
            <div className="chat-prose text-sm leading-relaxed text-fg-secondary">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{answer}</ReactMarkdown>
            </div>
          </div>
          {sources.length > 0 && (
            <div className="rounded-xl border border-border-light/70 bg-surface/40 p-3">
              <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-muted">引用来源</div>
              <ul className="space-y-1">
                {sources.map((s) => (
                  <li key={s.paper_id} className="flex items-start gap-1.5 text-xs text-fg-secondary">
                    <FileText className="mt-0.5 h-3 w-3 shrink-0 text-accent/70" />
                    <span className="truncate">{s.title || s.paper_id}</span>
                    {s.sections && s.sections.length > 0 && (
                      <span className="shrink-0 text-muted/70">[{s.sections.join(", ")}]</span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ---------- export_bibtex ---------- */

function BibtexCard({ result }: { result: Record<string, unknown> }) {
  const [expanded, setExpanded] = useState(true);
  const [copied, setCopied] = useState(false);
  const bibtex = ((result.citations || result.bibtex || "") as string);
  const isError = Boolean(result.error);
  const fmt = (result.format as string) || "bibtex";
  const ext = fmt === "gbt7714" ? "txt" : "bib";

  const handleCopy = async () => {
    await navigator.clipboard.writeText(bibtex);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  const handleDownload = () => {
    const blob = new Blob([bibtex], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `references.${ext}`; a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="my-2">
      <CardHeader name="citation_export" result={result} expanded={expanded}
        onToggle={() => setExpanded(!expanded)}
        meta_text={isError ? undefined : `${(result.count as number) || ""} 条 · ${fmt === "gbt7714" ? "GB/T 7714" : "BibTeX"}`} />
      {expanded && !isError && bibtex && (
        <div className="mt-1.5 space-y-2 pl-1">
          <div className="flex justify-end gap-1">
            <button onClick={handleCopy}
              className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium text-muted transition-colors hover:bg-surface-hover hover:text-fg">
              {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
              {copied ? "已复制" : "复制"}
            </button>
            <button onClick={handleDownload}
              className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium text-muted transition-colors hover:bg-surface-hover hover:text-fg">
              <Download className="h-3.5 w-3.5" />
              下载 .{ext}
            </button>
          </div>
          <pre className="max-h-[420px] overflow-auto rounded-xl border border-border-light bg-surface-sunken p-3.5 text-xs leading-relaxed text-fg-secondary">{bibtex}</pre>
        </div>
      )}
    </div>
  );
}

/* ---------- export_report ---------- */

function ReportCard({ result, name = "export_report" }: { result: Record<string, unknown>; name?: string }) {
  const [expanded, setExpanded] = useState(true);
  const files = (result.files || []) as { fileName: string; url?: string; size?: number }[];
  const isError = Boolean(result.error);

  return (
    <div className="my-2">
      <CardHeader name={name} result={result} expanded={expanded}
        onToggle={() => setExpanded(!expanded)}
        meta_text={isError ? undefined : `${files.length} 个文件`} />
      {expanded && !isError && files.length > 0 && (
        <ul className="mt-1.5 space-y-1.5 pl-1">
          {files.map((f) => (
            <li key={f.fileName}>
              <button type="button" onClick={() => {
                void downloadProtectedFile(f.url || `/files/${f.fileName}`, f.fileName);
              }}
                className="flex w-full items-center gap-2 rounded-lg border border-border-light bg-surface px-3 py-2 text-left text-xs text-fg-secondary transition-colors hover:bg-surface-hover">
                <Download className="h-3.5 w-3.5 shrink-0 text-accent" />
                <span className="font-medium">{f.fileName}</span>
                {f.size ? (
                  <span className="shrink-0 text-muted/60">{(f.size / 1024).toFixed(1)} KB</span>
                ) : null}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/* ---------- check_structure ---------- */

function StructureCheckCard({ result, name = "check_structure" }: { result: Record<string, unknown>; name?: string }) {
  const [expanded, setExpanded] = useState(true);
  const isError = Boolean(result.error);
  const report = (result.report as string) || "";
  const metrics = (result.metrics || {}) as Record<string, number>;
  const issues = (result.issues || []) as { severity: string }[];
  const hi = issues.filter((i) => i.severity === "high").length;
  const meta = isError ? undefined
    : `${metrics.section_count ?? "–"} 章节 · 问题 ${issues.length}${hi ? `（高 ${hi}）` : ""}`;
  return (
    <div className="my-2">
      <CardHeader name={name} result={result} expanded={expanded}
        onToggle={() => setExpanded(!expanded)} meta_text={meta} />
      {expanded && !isError && report && (
        <div className="mt-1.5 pl-1">
          <div className="max-h-[420px] overflow-y-auto rounded-xl border border-border-light bg-surface p-4">
            <div className="chat-prose text-sm leading-relaxed text-fg-secondary">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{report}</ReactMarkdown>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* ---------- integrity_sweep ---------- */

interface IntegrityRow {
  paper_id: string; title?: string; doi?: string; source?: string;
  status: string; note?: string;
}

const INTEGRITY_STATUS: Record<string, { label: string; badge: string; order: number }> = {
  retracted: { label: "撤稿", badge: "bg-error/15 text-error", order: 0 },
  concern: { label: "勘误/关切", badge: "bg-warning/15 text-warning", order: 1 },
  preprint_published: { label: "预印本", badge: "bg-accent-soft/60 text-accent", order: 2 },
  unknown: { label: "未查到", badge: "bg-muted/20 text-muted", order: 3 },
  clean: { label: "正常", badge: "bg-success/15 text-success", order: 4 },
};

function IntegritySweepCard({ result }: { result: Record<string, unknown> }) {
  const [expanded, setExpanded] = useState(true);
  const isError = Boolean(result.error);
  const report = (result.report || []) as IntegrityRow[];
  const facts = (result.facts || {}) as Record<string, string[]>;
  const nRisk = (facts.retracted || []).length + (facts.concern || []).length;
  const meta = isError ? undefined
    : `${report.length} 篇${nRisk ? ` · 风险 ${nRisk}` : ""}`;
  const rows = [...report].sort(
    (a, b) => (INTEGRITY_STATUS[a.status]?.order ?? 9) - (INTEGRITY_STATUS[b.status]?.order ?? 9));

  return (
    <div className="my-2">
      <CardHeader name="integrity_sweep" result={result} expanded={expanded}
        onToggle={() => setExpanded(!expanded)} meta_text={meta} />
      {expanded && !isError && rows.length > 0 && (
        <ul className="mt-1.5 space-y-1.5 pl-1">
          {rows.map((r) => {
            const st = INTEGRITY_STATUS[r.status] || INTEGRITY_STATUS.unknown;
            return (
              <li key={r.paper_id}
                className="rounded-lg border border-border-light bg-surface px-3 py-2">
                <div className="flex items-center gap-2">
                  <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${st.badge}`}>
                    {st.label}
                  </span>
                  <span className="truncate text-xs font-medium text-fg-secondary">
                    {r.title || r.paper_id}
                  </span>
                </div>
                {r.note && (
                  <p className="mt-1 text-[11px] leading-relaxed text-muted">{r.note}</p>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/* ---------- bib_import ---------- */

interface BibImportEntry { id: string; title?: string; doi?: string; verified?: boolean }
interface BibImportFailure { key?: string; reason: string }

function BibImportCard({ result }: { result: Record<string, unknown> }) {
  const [expanded, setExpanded] = useState(true);
  const isError = Boolean(result.error);
  const report = (result.report || {}) as { imported?: number; enriched?: number; failed?: number };
  const entries = (result.entries || []) as BibImportEntry[];
  const failures = ((report as { failures?: BibImportFailure[] }).failures || []) as BibImportFailure[];
  const meta = isError ? undefined
    : `${report.imported ?? 0} 篇导入${report.failed ? ` · ${report.failed} 跳过` : ""}`;
  return (
    <div className="my-2">
      <CardHeader name="bib_import" result={result} expanded={expanded}
        onToggle={() => setExpanded(!expanded)} meta_text={meta} />
      {expanded && !isError && entries.length > 0 && (
        <div className="mt-1.5 space-y-1.5 pl-1">
          <ul className="space-y-1">
            {entries.map((e) => (
              <li key={e.id} className="flex items-start gap-1.5 text-xs text-fg-secondary">
                <FileInput className="mt-0.5 h-3 w-3 shrink-0 text-accent/70" />
                <span className="truncate">{e.title || e.id}</span>
                {e.doi
                  ? <span className="shrink-0 text-success/70">DOI</span>
                  : <span className="shrink-0 text-muted/60">未校验</span>}
              </li>
            ))}
          </ul>
          {failures.length > 0 && (
            <div className="rounded-lg border border-border-light/60 bg-surface/40 p-2.5">
              <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted">
                跳过 {failures.length} 条
              </div>
              <ul className="space-y-0.5">
                {failures.slice(0, 8).map((f, i) => (
                  <li key={i} className="text-[11px] text-muted">
                    {f.key ? `${f.key}：` : ""}{f.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ---------- exhibit_index ---------- */

interface ExhibitCaption { num: string; type: string; caption: string; page?: string; section?: string }
interface ExhibitGroup { paper_id: string; title?: string; captions: ExhibitCaption[] }
interface ExhibitMissing { id: string; title?: string; reason?: string }

function ExhibitIndexCard({ result }: { result: Record<string, unknown> }) {
  const setComposerDraft = useUIStore((st) => st.setComposerDraft);
  const [expanded, setExpanded] = useState(true);
  const isError = Boolean(result.error);
  const groups = (result.exhibits || []) as ExhibitGroup[];
  const missing = (result.missing_attachments || []) as ExhibitMissing[];
  const total = groups.reduce((n, g) => n + g.captions.length, 0);
  const meta = isError ? undefined : `${total} 个图表 · ${groups.length} 篇`;

  return (
    <div className="my-2">
      <CardHeader name="exhibit_index" result={result} expanded={expanded}
        onToggle={() => setExpanded(!expanded)} meta_text={meta} />
      {expanded && !isError && groups.length > 0 && (
        <div className="mt-1.5 space-y-2 pl-1">
          {groups.map((g) => (
            <div key={g.paper_id} className="rounded-xl border border-border-light bg-surface p-2.5">
              <div className="mb-1.5 truncate text-xs font-medium text-fg-secondary">
                {g.title || g.paper_id}
                <span className="ml-1.5 text-muted/60">({g.captions.length})</span>
              </div>
              <ul className="space-y-1">
                {g.captions.map((c, i) => (
                  <li key={i}>
                    <button
                      onClick={() => setComposerDraft(
                        `针对「${c.num}」的 caption 追问：${c.caption.slice(0, 80)}`)}
                      className="flex w-full items-start gap-1.5 rounded-lg px-1.5 py-1 text-left text-[11px] leading-relaxed text-fg-secondary transition-colors hover:bg-surface-hover/60"
                    >
                      <span className={`mt-0.5 shrink-0 rounded px-1 py-0.5 text-[9px] font-medium ${
                        c.type === "table" ? "bg-accent-soft/60 text-accent" : "bg-warning/15 text-warning"}`}>
                        {c.type === "table" ? "表" : "图"}
                      </span>
                      <span className="shrink-0 font-medium text-fg/80">{c.num}</span>
                      {c.page && <span className="shrink-0 text-muted/50">p.{c.page}</span>}
                      <span className="line-clamp-2 text-muted">{c.caption}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ))}
          {missing.length > 0 && (
            <div className="rounded-lg border border-border-light/60 bg-surface/40 p-2.5">
              <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted">
                {missing.length} 个附件未解析
              </div>
              <ul className="space-y-0.5">
                {missing.slice(0, 8).map((m) => (
                  <li key={m.id} className="truncate text-[11px] text-muted">{m.title || m.id}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ---------- explain_element ---------- */

interface ElementDetail {
  element_id?: string; kind?: string; page?: number; section?: string;
  caption?: string; understanding?: Record<string, unknown>;
  docling_extract?: Record<string, unknown>; asset_url?: string | null;
}

function _undStr(u: Record<string, unknown>, key: string): string {
  const v = u[key];
  return typeof v === "string" ? v : "";
}
function _undList(u: Record<string, unknown>, key: string): string[] {
  const v = u[key];
  return Array.isArray(v) ? v.map((x) => String(x)).filter(Boolean) : [];
}

function ExplainElementCard({ result }: { result: Record<string, unknown> }) {
  const [expanded, setExpanded] = useState(true);
  const isError = Boolean(result.error);
  const element = (result.element || null) as ElementDetail | null;
  const [assetSrc, setAssetSrc] = useState("");
  useEffect(() => {
    let active = true;
    const url = element?.asset_url || "";
    if (!url) {
      setAssetSrc("");
      return () => { active = false; };
    }
    void fetchFileBlob(url).then((blob) => {
      if (active) setAssetSrc(URL.createObjectURL(blob));
    }).catch(() => {
      if (active) setAssetSrc("");
    });
    return () => {
      active = false;
      setAssetSrc((current) => {
        if (current) URL.revokeObjectURL(current);
        return "";
      });
    };
  }, [element?.asset_url]);
  if (!isError && !element) {
    return (
      <div className="my-2 rounded-xl border border-warning/30 bg-warning/5 p-3 text-xs text-fg-secondary">
        未找到该元素。请先上传论文文件并使用 exhibit_index 列出可用图表。
      </div>
    );
  }
  const kind = element?.kind || "";
  const u = (element?.understanding || {}) as Record<string, unknown>;
  const dl = (element?.docling_extract || {}) as Record<string, unknown>;
  const isTable = kind === "table";
  const isFormula = kind === "formula";
  const badge = isTable ? "表" : isFormula ? "式" : "图";
  const desc = _undStr(u, "description") || _undStr(u, "summary") || _undStr(u, "meaning");
  const components = _undList(u, "components").concat(_undList(u, "key_metrics"));
  const relations = _undList(u, "relations");
  const axes = _undList(u, "comparison_axes");
  const role = _undStr(u, "role") || _undStr(u, "role_in_paper");
  const tableMd = typeof dl.markdown === "string" ? dl.markdown : "";
  const latex = typeof dl.latex === "string" ? dl.latex : "";
  const variables = u.variables && typeof u.variables === "object"
    ? u.variables as Record<string, string> : {};
  const meta = element ? `${badge} · p.${element.page || "?"}${element.section ? ` · ${element.section}` : ""}` : undefined;

  return (
    <div className="my-2">
      <CardHeader name="explain_element" result={result} expanded={expanded}
        onToggle={() => setExpanded(!expanded)} meta_text={meta} />
      {expanded && !isError && element && (
        <div className="mt-1.5 space-y-2 pl-1">
          {element.caption && (
            <div className="rounded-lg border border-border-light bg-surface p-2.5 text-[13px] font-medium text-fg">
              {element.caption}
            </div>
          )}
          {/* Figure: thumbnail + VLM description */}
          {(assetSrc || element.asset_url) && (
            <img src={assetSrc || element.asset_url || ""} alt={element.caption || element.element_id}
              className="max-h-80 w-auto rounded-lg border border-border-light bg-surface object-contain"
              loading="lazy" />
          )}
          {desc && (
            <div className="text-sm text-fg-secondary">{desc}</div>
          )}
          {components.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {components.map((c, i) => (
                <span key={i} className="rounded bg-accent-soft/40 px-1.5 py-0.5 text-[11px] text-accent">{c}</span>
              ))}
            </div>
          )}
          {relations.length > 0 && (
            <ul className="ml-4 list-disc text-xs text-fg-secondary">
              {relations.map((r, i) => <li key={i}>{r}</li>)}
            </ul>
          )}
          {/* Table: structured markdown */}
          {isTable && tableMd && (
            <div className="overflow-x-auto rounded-lg border border-border-light bg-surface p-2.5">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{tableMd}</ReactMarkdown>
            </div>
          )}
          {axes.length > 0 && (
            <div className="text-xs text-muted">比较维度：{axes.join(" / ")}</div>
          )}
          {/* Formula: LaTeX source (monospace; no KaTeX dependency) */}
          {isFormula && latex && (
            <pre className="overflow-x-auto rounded-lg border border-border-light bg-surface p-2.5 text-[13px] leading-relaxed text-fg">
              <code>{latex}</code>
            </pre>
          )}
          {Object.keys(variables).length > 0 && (
            <ul className="ml-4 list-disc text-xs text-fg-secondary">
              {Object.entries(variables).map(([sym, meaning]) => (
                <li key={sym}><code className="text-fg">{sym}</code> — {meaning}</li>
              ))}
            </ul>
          )}
          {role && (
            <div className="text-xs text-muted">作用：{role}</div>
          )}
          {!desc && !tableMd && !latex && (
            <div className="text-xs text-muted">该元素暂无 VLM 语义解读（可能未配置多模态模型或解析时被跳过）。</div>
          )}
        </div>
      )}
    </div>
  );
}

/* ---------- field_census ---------- */

interface CensusBucket { key: string; name: string; count: number }

function FieldCensusTrend({ yearly }: { yearly: CensusBucket[] }) {
  // Pure deterministic layout: year index → x, count → y scaled to max.
  const W = 640, H = 200, PAD_L = 10, PAD_R = 14, PAD_T = 14, PAD_B = 26;
  if (yearly.length === 0) return null;
  const innerW = W - PAD_L - PAD_R;
  const innerH = H - PAD_T - PAD_B;
  const max = Math.max(...yearly.map((d) => d.count), 1);
  const xAt = (i: number) => PAD_L + (yearly.length === 1 ? innerW / 2 : (i / (yearly.length - 1)) * innerW);
  const yAt = (c: number) => PAD_T + innerH - (c / max) * innerH;
  const points = yearly.map((d, i) => `${xAt(i)},${yAt(d.count)}`).join(" ");
  const gridY = [0, 0.5, 1].map((f) => PAD_T + innerH - f * innerH);
  const step = Math.max(1, Math.ceil(yearly.length / 8));
  const labelIdx = yearly.map((_, i) => i).filter((i) => i % step === 0 || i === yearly.length - 1);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ aspectRatio: `${W} / ${H}` }}>
      {gridY.map((y, i) => (
        <line key={i} x1={PAD_L} y1={y} x2={W - PAD_R} y2={y}
          stroke="rgb(var(--border-light))" strokeWidth={1} strokeDasharray="2 3" />
      ))}
      <polyline points={points} fill="none" stroke="rgb(var(--accent))" strokeWidth={2}
        strokeLinejoin="round" strokeLinecap="round" />
      {yearly.map((d, i) => (
        <circle key={i} cx={xAt(i)} cy={yAt(d.count)} r={2.5} fill="rgb(var(--accent))" />
      ))}
      {labelIdx.map((i) => (
        <text key={i} x={xAt(i)} y={H - 8} textAnchor="middle"
          fontSize={10} fill="rgb(var(--muted))">{yearly[i].key}</text>
      ))}
    </svg>
  );
}

function CensusTopList({ title, rows }: { title: string; rows: CensusBucket[] }) {
  if (rows.length === 0) return null;
  const top = Math.max(...rows.map((r) => r.count), 1);
  return (
    <div>
      <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted">{title}</div>
      <ul className="space-y-1">
        {rows.map((r, i) => (
          <li key={r.key || i} className="flex items-center gap-2 text-xs">
            <span className="w-4 shrink-0 text-muted/60">{i + 1}</span>
            <span className="w-40 shrink-0 truncate text-fg-secondary">{r.name}</span>
            <span className="relative h-1.5 flex-1 overflow-hidden rounded bg-border-light/60">
              <span className="absolute inset-y-0 left-0 rounded bg-accent/70"
                style={{ width: `${(r.count / top) * 100}%` }} />
            </span>
            <span className="w-10 shrink-0 text-right tabular-nums text-muted">{r.count}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function FieldCensusCard({ result }: { result: Record<string, unknown> }) {
  const [expanded, setExpanded] = useState(true);
  const isError = Boolean(result.error);
  const yearly = (result.yearly || []) as CensusBucket[];
  const authors = (result.top_authors || []) as CensusBucket[];
  const insts = (result.top_institutions || []) as CensusBucket[];
  const venues = (result.top_venues || []) as CensusBucket[];
  const portrait = (result.portrait || "") as string;
  const meta = isError ? undefined : `${yearly.length} 年 · 作者 ${authors.length}`;
  return (
    <div className="my-2">
      <CardHeader name="field_census" result={result} expanded={expanded}
        onToggle={() => setExpanded(!expanded)} meta_text={meta} />
      {expanded && !isError && (yearly.length > 0 || authors.length > 0) && (
        <div className="mt-1.5 space-y-3 pl-1">
          {portrait && (
            <div className="rounded-xl border border-border-light bg-surface p-3 text-xs leading-relaxed text-fg-secondary">
              {portrait}
            </div>
          )}
          {yearly.length > 0 && (
            <div className="rounded-xl border border-border-light bg-surface p-3">
              <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted">
                年度发文趋势
              </div>
              <FieldCensusTrend yearly={yearly} />
            </div>
          )}
          <div className="grid gap-3 sm:grid-cols-1">
            <CensusTopList title="高产作者 Top" rows={authors} />
            <CensusTopList title="高产机构 Top" rows={insts} />
            <CensusTopList title="主要刊物 Top" rows={venues} />
          </div>
        </div>
      )}
    </div>
  );
}

/* ---------- generic tool card ---------- */

function ToolCard({ name, result }: { name: string; result?: unknown }) {
  const [expanded, setExpanded] = useState(false);
  const toolName = LEGACY_TOOL_NAMES[name] || name;
  const r = (result || {}) as Record<string, unknown>;

  if (toolName === "search_papers" && (r.papers || r.error)) return <SearchResultCard result={r} />;
  if (toolName === "research_map" && (r.clusters || r.error)) return <ResearchMapCard result={r} />;
  if (toolName === "reading_path" && (r.path || r.error)) return <ReadingPathCard result={r} />;
  if (toolName === "write_review" && (r.literature_review || r.error)) return <ReviewCard result={r} />;
  if (toolName === "ask_papers" && (r.answer || r.error)) return <AnswerCard result={r} />;
  if (toolName === "deep_read" && (r.summaries || r.attachments || r.error)) return <DeepReadCard result={r} />;
  if (toolName === "citation_export" && (r.citations || r.bibtex || r.error)) return <BibtexCard result={r} />;
  if (toolName === "export_report" && (r.files || r.error)) return <ReportCard result={r} />;
  if (toolName === "check_structure" && (r.report || r.error)) return <StructureCheckCard result={r} />;
  if (toolName === "check_format" && (r.report || r.error)) return <StructureCheckCard result={r} name="check_format" />;
  if (toolName === "export_manuscript" && (r.files || r.error)) return <ReportCard result={r} name="export_manuscript" />;
  if (toolName === "integrity_sweep" && (r.report || r.error)) return <IntegritySweepCard result={r} />;
  if (toolName === "bib_import" && (r.report || r.entries || r.error)) return <BibImportCard result={r} />;
  if (toolName === "exhibit_index" && (r.exhibits || r.missing_attachments || r.error)) return <ExhibitIndexCard result={r} />;
  if (toolName === "explain_element" && (r.element !== undefined || r.error)) return <ExplainElementCard result={r} />;
  if (toolName === "field_census" && (r.yearly || r.top_authors || r.error)) return <FieldCensusCard result={r} />;

  const meta_text = typeof r.summary === "string" ? r.summary : undefined;
  return (
    <div className="group/tool my-1.5">
      <CardHeader name={toolName} result={result} expanded={expanded}
        onToggle={() => setExpanded(!expanded)} meta_text={meta_text} />
      {expanded && result != null && (
        <pre className="ml-9 mt-0.5 overflow-x-auto rounded-lg bg-surface-sunken p-2.5 text-[11px] leading-relaxed text-muted">
          {JSON.stringify(result, null, 2).slice(0, 600)}
        </pre>
      )}
    </div>
  );
}

/* ---------- thinking / avatar / messages ---------- */

function ThinkingBlock({ text, isStreaming }: { text: string; isStreaming: boolean }) {
  const [open, setOpen] = useState(isStreaming);

  return (
    <div className="mb-2">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 py-1 text-xs transition-colors hover:opacity-80"
      >
        {isStreaming ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin text-accent" />
        ) : (
          <Brain className="h-3.5 w-3.5 text-muted/60" />
        )}
        <span className={`font-medium ${isStreaming ? "text-accent" : "text-muted"}`}>
          {isStreaming ? "思考中" : "已深度思考"}
        </span>
        {!isStreaming && <span className="text-muted/50">· 点击查看</span>}
        <ChevronDown className={`h-3 w-3 text-muted/50 transition-transform ${open ? "" : "-rotate-90"}`} />
      </button>
      {open && (
        <div className="mt-1.5 rounded-xl bg-surface-hover/40 px-3.5 py-2.5">
          <p className="whitespace-pre-wrap text-[13px] leading-[1.7] text-muted">{text}</p>
        </div>
      )}
    </div>
  );
}

function Avatar({ role }: { role: "user" | "assistant" }) {
  if (role === "user") {
    return (
      <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-fg-secondary to-fg text-[11px] font-medium text-white">
        我
      </div>
    );
  }
  return (
    <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-accent to-accent-hover shadow-sm">
      <Sparkles className="h-3.5 w-3.5 text-white" />
    </div>
  );
}

// Memoized: during streaming the deltas live in the store's current*
// fields, so historical `msg` objects keep identity and every old message
// (with its ReactMarkdown / ToolCard / GenealogyGraph) skips re-rendering
// entirely on each streamed token. Without this the whole list re-parses
// markdown per token — the main source of page jank.
export const ChatMessage = memo(function ChatMessage({ msg, isLast, disabled, onRegenerate }: {
  msg: ChatMsg; isLast?: boolean; disabled?: boolean; onRegenerate?: () => void;
}) {
  const isUser = msg.role === "user";

  if (isUser) {
    return (
      <div className="flex items-start justify-end gap-2.5 px-1 py-3">
        <div className="flex max-w-[70%] flex-col items-end">
          <div className="rounded-[18px] rounded-tr-md bg-accent px-4 py-2.5 text-[14px] leading-[1.6] text-white shadow-sm">
            <p className="whitespace-pre-wrap">{msg.content}</p>
          </div>
          {msg.attachments && msg.attachments.length > 0 && (
            <div className="mt-1.5 flex flex-wrap justify-end gap-1.5">
              {msg.attachments.map((a) => (
                <FileChip key={a.id} attach={a} />
              ))}
            </div>
          )}
        </div>
        <Avatar role="user" />
      </div>
    );
  }

  return (
    <div className="group flex items-start gap-2.5 px-1 py-3">
      <Avatar role="assistant" />
      <div className="flex min-w-0 flex-1 flex-col">
        {msg.thinking && <ThinkingBlock text={msg.thinking} isStreaming={!!msg.isStreaming} />}
        {msg.toolCalls?.filter((tc) => tc.name !== "use_skill").map((tc, i) => (
          <ToolCard key={i} name={tc.name} result={tc.result} />
        ))}
        {msg.content && (
          <div className="py-0.5">
            <div className="chat-prose">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
            </div>
            <AssistantActions msg={msg} isLast={isLast} disabled={disabled} onRegenerate={onRegenerate} />
          </div>
        )}
      </div>
    </div>
  );
});

export function StreamingMessage({
  thinking,
  answer,
  activeTool,
  toolProgress,
  toolCalls,
  currentStep,
  heartbeatElapsed,
}: {
  thinking: string;
  answer: string;
  activeTool: string | null;
  toolProgress: string[];
  toolCalls: { name: string; result?: unknown }[];
  currentStep: string | null;
  heartbeatElapsed: number;
}) {
  const hasContent = thinking || answer || activeTool || toolCalls.length > 0 || currentStep;

  const STEP_LABELS: Record<string, string> = {
    thinking: "正在思考",
    tool_executing: "正在执行工具",
  };
  const stepLabel = currentStep ? STEP_LABELS[currentStep] || currentStep : null;

  return (
    <div className="flex items-start gap-2.5 px-1 py-3">
      <Avatar role="assistant" />
      <div className="flex min-w-0 flex-1 flex-col">
        {!thinking && !answer && !activeTool && stepLabel && (
          <div className="flex items-center gap-2 py-1">
            <Loader2 className="h-3.5 w-3.5 animate-spin text-accent" />
            <span className="text-[13px] text-muted">{stepLabel}...</span>
            {heartbeatElapsed > 0 && (
              <span className="tnum text-[11px] text-muted/50">{heartbeatElapsed}s</span>
            )}
          </div>
        )}
        {thinking && <ThinkingBlock text={thinking} isStreaming={true} />}
        {activeTool && (
          <div className="my-1.5 flex items-center gap-2.5 rounded-xl px-3 py-2">
            <div className="flex h-6 w-6 items-center justify-center rounded-md bg-accent-soft/60">
              <Loader2 className="h-3.5 w-3.5 animate-spin text-accent" />
            </div>
            <span className="text-[13px] text-muted">
              {toolProgress.length > 0
                ? toolProgress[toolProgress.length - 1]
                : `正在执行 ${(TOOL_META[activeTool] || {}).label || activeTool}...`}
            </span>
            {heartbeatElapsed > 0 && (
              <span className="tnum ml-auto text-[11px] text-muted/50">{heartbeatElapsed}s</span>
            )}
          </div>
        )}
        {toolCalls.filter((tc) => tc.name !== "use_skill").map((tc, i) => (
          <ToolCard key={i} name={tc.name} result={tc.result} />
        ))}
        {answer && (
          <div className="py-0.5">
            <div className="chat-prose">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{answer}</ReactMarkdown>
              <span className="ml-0.5 inline-block h-4 w-0.5 animate-pulse bg-accent align-middle" />
            </div>
          </div>
        )}
        {!hasContent && (
          <div className="flex items-center gap-1.5 py-1">
            <span className="h-2 w-2 animate-bounce rounded-full bg-muted/40 [animation-delay:-0.3s]" />
            <span className="h-2 w-2 animate-bounce rounded-full bg-muted/40 [animation-delay:-0.15s]" />
            <span className="h-2 w-2 animate-bounce rounded-full bg-muted/40" />
          </div>
        )}
      </div>
    </div>
  );
}
