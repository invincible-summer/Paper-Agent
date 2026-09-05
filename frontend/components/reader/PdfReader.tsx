"use client";
import { useEffect, useRef, useState, useCallback } from "react";
import type { PDFDocumentProxy, PDFPageProxy, TextLayer, RenderTask } from "pdfjs-dist";
import type { Anchor, Selection } from "@/lib/reader-api";
import { authHeaders } from "@/lib/auth";

interface Props {
  url: string; page: number; zoom: number; view: "continuous" | "single" | "spread";
  selection: Selection | null; regionMode: boolean; savedAnchorIds: string[];
  jump: { page: number; serial: number }; anchors: Anchor[]; activeAnchor?: string;
  onPage: (page: number) => void; onSelection: (value: Selection) => void;
  onAnchor: (anchor: Anchor) => void;
}

export function PdfReader(props: Props) {
  const [doc, setDoc] = useState<PDFDocumentProxy | null>(null);
  const [error, setError] = useState("");
  const [width, setWidth] = useState(800);
  const root = useRef<HTMLDivElement>(null);
  const lastScroll = useRef(0);
  useEffect(() => {
    let disposed = false;
    let task: ReturnType<typeof import("pdfjs-dist")["getDocument"]> | undefined;
    setDoc(null); setError("");
    void import("pdfjs-dist").then(pdf => {
      if (disposed) return;
      pdf.GlobalWorkerOptions.workerSrc = "/pdfjs/pdf.worker.min.mjs";
      task = pdf.getDocument({ url: props.url, httpHeaders: authHeaders(),
        cMapUrl: "/pdfjs/cmaps/", cMapPacked: true, standardFontDataUrl: "/pdfjs/standard_fonts/",
        wasmUrl: "/pdfjs/wasm/", enableXfa: false });
      return task.promise.then(value => { if (!disposed) setDoc(value); });
    }).catch(() => { if (!disposed) setError("PDF 原件加载失败，请刷新重试；加密文件请先解锁。"); });
    return () => { disposed = true; void task?.destroy(); };
  }, [props.url]);
  useEffect(() => {
    if (!root.current) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(root.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!doc) return;
    if (props.view !== "continuous") { root.current?.scrollTo({ top: 0, left: 0 }); return; }
    const timer = setTimeout(() => root.current?.querySelector(`[data-pdf-page="${props.jump.page}"]`)
      ?.scrollIntoView({ block: "start", behavior: "instant" as ScrollBehavior }), 50);
    return () => clearTimeout(timer);
  }, [doc, props.jump, props.view]);
  const onScroll = () => {
    if (props.view !== "continuous" || Date.now() - lastScroll.current < 150) return;
    lastScroll.current = Date.now();
    const element = root.current;
    if (!element) return;
    const top = element.getBoundingClientRect().top;
    let selected = props.page;
    for (const node of Array.from(element.querySelectorAll<HTMLElement>("[data-pdf-page]"))) {
      const box = node.getBoundingClientRect();
      if (box.bottom > top + 80 && box.top < top + element.clientHeight) {
        selected = Number(node.dataset.pdfPage); break;
      }
    }
    if (selected !== props.page) props.onPage(selected);
  };
  const onSelect = () => {
    if (props.regionMode) return;
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return;
    const range = selection.getRangeAt(0);
    const parent = range.startContainer.parentElement?.closest<HTMLElement>("[data-pdf-page]");
    const end = range.endContainer.parentElement?.closest<HTMLElement>("[data-pdf-page]");
    if (!parent || !root.current?.contains(parent)) return;
    if (parent !== end) { setError("请一次选择同一页内的文字；跨页内容可以分段记下。"); return; }
    const text = selection.toString().trim();
    if (!text) return;
    if (text.length > 6000) { setError("选文较长，请分段选择（每段最多 6000 字）。"); return; }
    setError("");
    const box = parent.getBoundingClientRect();
    const rotation = Number(parent.dataset.rotation || 0);
    const rects = Array.from(range.getClientRects()).filter(r => r.width > 1 && r.height > 1).slice(0, 100).map(r => {
      const corners = [[(r.left - box.left) / box.width, (r.top - box.top) / box.height],
        [(r.right - box.left) / box.width, (r.bottom - box.top) / box.height]];
      const points = corners.map(([x, y]) => rotation === 90 ? [y, 1 - x] : rotation === 180 ? [1 - x, 1 - y] : rotation === 270 ? [1 - y, x] : [x, y]);
      return [Math.min(...points.map(p => p[0])), Math.min(...points.map(p => p[1])),
        Math.max(...points.map(p => p[0])), Math.max(...points.map(p => p[1]))].map(v => Math.max(0, Math.min(1, v)));
    });
    props.onSelection({ page: Number(parent.dataset.pdfPage), quote: text, rects });
    selection.removeAllRanges();
  };
  const pages = doc ? props.view === "continuous" ? Array.from({ length: doc.numPages }, (_, i) => i + 1)
    : props.view === "spread" && width > 900 ? [props.page, props.page + 1].filter(p => p <= doc.numPages) : [props.page] : [];
  const spread = props.view === "spread" && width > 900;
  const pageWidth = Math.max(250, Math.min(spread ? (width - 72) / 2 : width - 56, 1050)) * props.zoom;
  const onPage = props.onPage;
  const go = useCallback(async (destination: string | unknown[]) => {
    if (!doc) return;
    const dest = typeof destination === "string" ? await doc.getDestination(destination) : destination;
    if (!dest) return;
    const first = dest[0];
    const index = typeof first === "number" ? first : await doc.getPageIndex(first as { num: number; gen: number });
    onPage(index + 1);
    root.current?.querySelector(`[data-pdf-page="${index + 1}"]`)?.scrollIntoView({ block: "start" });
  }, [doc, onPage]);
  return <div className="reader-paper-scroll" ref={root} onScroll={onScroll} onMouseUp={onSelect} onKeyUp={onSelect} onTouchEnd={() => setTimeout(onSelect, 100)}>
    {error && <div className="reader-error" role="alert">{error}</div>}
    {!doc && !error && <div className="reader-loading"><span className="reader-spinner" />正在展开论文原页…<small>无需等待 AI 分析</small></div>}
    <div className={`reader-pages ${spread ? "reader-spread" : ""}`}>
      {doc && pages.map(number => <PdfPage key={number} document={doc} number={number} width={pageWidth}
        anchors={props.anchors.filter(a => a.page === number)} activeAnchor={props.activeAnchor}
        selection={props.selection?.page === number ? props.selection : null} regionMode={props.regionMode} savedAnchorIds={props.savedAnchorIds} onSelection={props.onSelection}
        onAnchor={props.onAnchor} onDestination={go} />)}
    </div>
  </div>;
}

function PdfPage({ document: doc, number, width, anchors, activeAnchor, onAnchor, onDestination, selection, regionMode, savedAnchorIds, onSelection }: {
  selection: Selection | null; regionMode: boolean; savedAnchorIds: string[]; onSelection: (value: Selection) => void;
  document: PDFDocumentProxy; number: number; width: number; anchors: Anchor[]; activeAnchor?: string;
  onAnchor: (anchor: Anchor) => void; onDestination: (destination: string | unknown[]) => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  const drag = useRef<number[] | null>(null);
  const [draftRect, setDraftRect] = useState<number[] | null>(null);
  useEffect(() => { drag.current = null; setDraftRect(null); }, [regionMode, width]);
  const canvas = useRef<HTMLCanvasElement>(null);
  const text = useRef<HTMLDivElement>(null);
  const [near, setNear] = useState(false);
  const [ratio, setRatio] = useState(1.4142);
  const [rotation, setRotation] = useState(0);
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState(false);
  const [noText, setNoText] = useState(false);
  const [links, setLinks] = useState<{ rect: number[]; url?: string; dest?: string | unknown[] }[]>([]);
  useEffect(() => {
    if (!root.current) return;
    const observer = new IntersectionObserver(([entry]) => setNear(entry.isIntersecting), { rootMargin: "900px" });
    observer.observe(root.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!near) { setReady(false); return; }
    let disposed = false;
    const textNode = text.current;
    const canvasNode = canvas.current;
    let render: RenderTask | undefined;
    let layer: TextLayer | undefined;
    let page: PDFPageProxy | undefined;
    setReady(false); setFailed(false);
    void (async () => {
      const pdf = await import("pdfjs-dist");
      page = await doc.getPage(number);
      if (disposed || !canvas.current || !text.current) return;
      const base = page.getViewport({ scale: 1 });
      const viewport = page.getViewport({ scale: width / base.width });
      setRatio(viewport.height / viewport.width); setRotation(page.rotate);
      const scale = Math.min(window.devicePixelRatio || 1, 2);
      canvas.current.width = Math.round(viewport.width * scale);
      canvas.current.height = Math.round(viewport.height * scale);
      canvas.current.style.width = `${viewport.width}px`;
      canvas.current.style.height = `${viewport.height}px`;
      render = page.render({ canvas: canvas.current, viewport, transform: [scale, 0, 0, scale, 0, 0] });
      await render.promise;
      if (disposed) return;
      text.current.replaceChildren();
      text.current.style.setProperty("--scale-factor", String(viewport.scale));
      text.current.style.setProperty("--total-scale-factor", String(viewport.scale * page.userUnit));
      const content = await page.getTextContent();
      if (disposed) return;
      setNoText(!content.items.some(item => "str" in item && item.str.trim()));
      layer = new pdf.TextLayer({ textContentSource: content, container: text.current, viewport });
      await layer.render();
      const annotations = await page.getAnnotations();
      if (disposed) return;
      setLinks(annotations.filter(a => a.subtype === "Link" && (a.url || a.dest)).map(a => {
        const r = [...viewport.convertToViewportPoint(a.rect[0], a.rect[1]), ...viewport.convertToViewportPoint(a.rect[2], a.rect[3])];
        return { rect: [Math.min(r[0], r[2]) / width, Math.min(r[1], r[3]) / viewport.height,
          Math.abs(r[2] - r[0]) / width, Math.abs(r[3] - r[1]) / viewport.height],
          url: /^https?:\/\//i.test(a.url || "") ? a.url : undefined, dest: a.dest };
      }));
      setReady(true);
    })().catch(e => { if (!disposed && e?.name !== "RenderingCancelledException") setFailed(true); });
    return () => {
      disposed = true; render?.cancel(); layer?.cancel();
      textNode?.replaceChildren();
      if (canvasNode) { canvasNode.width = 0; canvasNode.height = 0; }
      page?.cleanup();
    };
  }, [doc, near, number, width]);
  const rotateRect = (r: number[]) => {
    if (rotation === 90) return [1 - r[3], r[0], 1 - r[1], r[2]];
    if (rotation === 180) return [1 - r[2], 1 - r[3], 1 - r[0], 1 - r[1]];
    if (rotation === 270) return [r[1], 1 - r[2], r[3], 1 - r[0]];
    return r;
  };
  return <section className="reader-page-wrap" style={{ width }} aria-label={`PDF 第 ${number} 页`}>
    <div ref={root} data-pdf-page={number} data-rotation={rotation} className="reader-pdf-page" style={{ width, height: width * ratio }}>
      {!ready && <div className="reader-page-placeholder">{failed ? "此页渲染失败，请重新打开论文" : `第 ${number} 页`}</div>}
      <canvas ref={canvas} aria-hidden="true" />
      <div className="reader-highlights" aria-hidden="true">{anchors.filter(a => savedAnchorIds.includes(a.id) || a.id === activeAnchor).flatMap(a => a.rects.map((raw, i) => {
        const r = rotateRect(raw);
        return <span key={`${a.id}-${i}`} className={`${a.id === activeAnchor ? "active" : ""} ${savedAnchorIds.includes(a.id) ? "saved" : "temporary"} ${!a.quote ? "region" : ""}`}
          style={{ left: `${r[0] * 100}%`, top: `${r[1] * 100}%`, width: `${(r[2] - r[0]) * 100}%`, height: `${(r[3] - r[1]) * 100}%` }} />;
      }))}</div>
      <div className="reader-current-selection" aria-hidden="true">{(draftRect ? [draftRect] : selection?.rects.map(rotateRect) || []).map((r, i) => <span key={i} className={draftRect || !selection?.quote ? "region" : ""}
        style={{ left: `${r[0] * 100}%`, top: `${r[1] * 100}%`, width: `${(r[2] - r[0]) * 100}%`, height: `${(r[3] - r[1]) * 100}%` }} />)}</div>
      <div ref={text} className="textLayer reader-text-layer" />
      {regionMode && <div className="reader-region-selector" role="img" aria-label="拖动框选图片、表格或公式区域"
        onPointerDown={e => { if (e.button !== 0) return; e.preventDefault(); e.currentTarget.setPointerCapture(e.pointerId);
          const b = e.currentTarget.getBoundingClientRect(); drag.current = [(e.clientX - b.left) / b.width, (e.clientY - b.top) / b.height]; setDraftRect(null); }}
        onPointerMove={e => { if (!drag.current) return; const b = e.currentTarget.getBoundingClientRect();
          const x = Math.max(0, Math.min(1, (e.clientX - b.left) / b.width)), y = Math.max(0, Math.min(1, (e.clientY - b.top) / b.height));
          setDraftRect([Math.min(x, drag.current[0]), Math.min(y, drag.current[1]), Math.max(x, drag.current[0]), Math.max(y, drag.current[1])]); }}
        onPointerCancel={() => { drag.current = null; setDraftRect(null); }}
        onPointerUp={e => { if (!drag.current) return; const b = e.currentTarget.getBoundingClientRect();
          const x = Math.max(0, Math.min(1, (e.clientX - b.left) / b.width)), y = Math.max(0, Math.min(1, (e.clientY - b.top) / b.height));
          const r = [Math.min(x, drag.current[0]), Math.min(y, drag.current[1]), Math.max(x, drag.current[0]), Math.max(y, drag.current[1])];
          drag.current = null; setDraftRect(null); if ((r[2] - r[0]) * b.width < 8 || (r[3] - r[1]) * b.height < 8) return;
          const points = [[r[0], r[1]], [r[2], r[3]]].map(([a, c]) => rotation === 90 ? [c, 1-a] : rotation === 180 ? [1-a, 1-c] : rotation === 270 ? [1-c, a] : [a, c]);
          onSelection({page: number, quote: "", rects: [[Math.min(...points.map(p => p[0])), Math.min(...points.map(p => p[1])), Math.max(...points.map(p => p[0])), Math.max(...points.map(p => p[1]))]]}); }} />}

      {links.map((link, i) => <a key={i} className="reader-pdf-link" href={link.url || "#"}
        target={link.url ? "_blank" : undefined} rel="noopener noreferrer" aria-label={link.url ? "打开论文链接" : "跳转论文引用"}
        onClick={e => { if (!link.url) { e.preventDefault(); if (link.dest) onDestination(link.dest); } }}
        style={{ left: `${link.rect[0] * 100}%`, top: `${link.rect[1] * 100}%`, width: `${link.rect[2] * 100}%`, height: `${link.rect[3] * 100}%` }} />)}
      <div className="reader-page-marks">{anchors.map((a, i) => <button key={a.id} title={`阅读标记：${a.quote.slice(0, 70) || '整页'}`}
        onClick={() => onAnchor(a)} className={a.id === activeAnchor ? "active" : ""}>{i + 1}</button>)}</div>
    </div>
    <footer>— {number} —{ready && noText && <span>扫描页 · 可按页提问与记笔记</span>}</footer>
  </section>;
}
