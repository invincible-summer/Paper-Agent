"use client";
import { useMemo, useRef, useState } from "react";
import type { GenealogyGraph as GraphData, GraphNode } from "@/lib/types";
import { layoutGenealogy, resolveEdges, type PositionedNode } from "@/lib/genealogy-layout";
import { useUIStore } from "@/stores/ui";

/* 论文谱系图 v2 — 固定画布 + 确定性布局（纯 SVG，零依赖）
 *
 * - 布局全部在 lib/genealogy-layout.ts 纯函数中完成：920px 固定内容宽、
 *   年份等距刻度、泳道按簇大小降序且高度随最大 (簇, 年) 桶自适应——
 *   每篇论文都是独立节点，不再折叠为 "+N" 聚合点。
 * - 视口 920×560（svg 按同比例自适应容器宽，无留白边带）：内容自动
 *   双轴适配缩放（放大 ≤2×）并居中占满画幅；内容偏矮时先以更窄画布
 *   重排再放大，避免横向裁切。拖拽平移（拖过阈值不触发点击），
 *   右下角 ＋/− 按钮绕视口中心缩放；滚轮不干预，滚动照常翻页。
 * - 悬停节点 → 气泡卡片显示论文标题/年份/被引/主题簇。
 * - 点击节点 → 下方详情面板：元信息、摘要片段、引用关系双列（可跳转）、
 *   打开原文、「深问这篇」（预填聊天输入框，打通图谱 → RAG 问答）。
 * - 点击边 → 下方关系面板：引用方向（谁引用了谁）或语义相似度，
 *   两端论文卡片可点击跳转。
 * - 点击节点同时进入聚焦模式：引用子树高亮，祖先链按年份渐变粗细
 *  （越老的思想源流边越粗），直观呈现"思想从哪流过来"。
 * - 顶部簇筛选 chips / 候选集开关 / 语义边开关；底部谱系摘要统计行。
 */

// 苔绿-赭石系（与全站「阅研纸感」主题同源）：首色=主色橄榄绿，次色=赭石点缀。
const CLUSTER_COLORS = [
  "#476d3a", "#b77837", "#315e4e", "#4a628a", "#6b4a8a", "#3b7a4a",
  "#a05a2c", "#8a6d3b",
];
const VIEW_W = 920;
const VIEW_H = 560;
const MAX_UPSCALE = 2.0; // fill the frame for short graphs without going comically large

type Selection =
  | { kind: "paper"; id: string }
  | { kind: "edge"; source: string; target: string; etype: string }
  | null;

function edgePath(s: { x: number; y: number }, t: { x: number; y: number }): string {
  const mx = (s.x + t.x) / 2;
  return `M ${s.x} ${s.y} C ${mx} ${s.y}, ${mx} ${t.y}, ${t.x} ${t.y}`;
}

/** Rough text width estimate: CJK ≈ 1em, latin/digit ≈ 0.56em. */
function estTextWidth(s: string, fs: number): number {
  let w = 0;
  for (const ch of s) w += (ch.codePointAt(0) ?? 0) > 0x2e7f ? fs : fs * 0.56;
  return w;
}

function truncateToWidth(s: string, max: number, fs: number): string {
  let w = 0, out = "";
  for (const ch of s) {
    w += (ch.codePointAt(0) ?? 0) > 0x2e7f ? fs : fs * 0.56;
    if (w > max) return out + "…";
    out += ch;
  }
  return out;
}

const ROLE_LABEL: Record<string, string> = { foundational: "奠基", bridge: "桥梁" };

export function GenealogyGraph({ data, clusterLabels }: { data: GraphData; clusterLabels?: Record<number, string> }) {
  const [selection, setSelection] = useState<Selection>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const [showSemantic, setShowSemantic] = useState(true);
  const [showCandidates, setShowCandidates] = useState(true);
  const [hiddenClusters, setHiddenClusters] = useState<Set<number>>(new Set());
  // dx/dy = user pan offset from the centered position; k = user zoom factor.
  const [view, setView] = useState({ dx: 0, dy: 0, k: 1 });
  const svgRef = useRef<SVGSVGElement | null>(null);
  const dragRef = useRef<{ startX: number; startY: number; x: number; y: number; moved: boolean } | null>(null);
  const suppressClick = useRef(false);
  const setComposerDraft = useUIStore((st) => st.setComposerDraft);

  // Full node info by id.
  const fullById = useMemo(
    () => new Map((data.nodes || []).map((n) => [n.id, n])), [data.nodes]);

  // Cluster filter chips derived from the unfiltered data.
  const clusterChips = useMemo(() => {
    const counts = new Map<number, number>();
    for (const n of data.nodes || []) counts.set(n.cluster, (counts.get(n.cluster) || 0) + 1);
    return Array.from(counts.entries()).sort((a, b) => b[1] - a[1] || a[0] - b[0]);
  }, [data.nodes]);

  // Filtered data -> deterministic layout.
  const filtered = useMemo<GraphData>(() => {
    const nodes = (data.nodes || []).filter(
      (n) => !hiddenClusters.has(n.cluster) && (showCandidates || n.layer === "core"));
    const ids = new Set(nodes.map((n) => n.id));
    return { nodes, edges: (data.edges || []).filter((e) => ids.has(e.source) && ids.has(e.target)) };
  }, [data, hiddenClusters, showCandidates]);

  // Two-pass layout: short graphs get re-laid on a narrower canvas so the
  // auto-fit can scale them up to fill the frame without cropping the sides.
  const layout = useMemo(() => {
    const first = layoutGenealogy(filtered, { width: VIEW_W });
    if (first.height > 0 && first.height < VIEW_H - 12) {
      const w2 = Math.min(VIEW_W, Math.max(480, Math.round(first.height * (VIEW_W / VIEW_H))));
      if (w2 < VIEW_W) return layoutGenealogy(filtered, { width: w2 });
    }
    return first;
  }, [filtered]);
  const drawnEdges = useMemo(
    () => resolveEdges(filtered.edges || []), [filtered.edges]);

  const pointById = useMemo(() => {
    const m = new Map<string, { x: number; y: number }>();
    for (const n of layout.nodes) m.set(n.id, n);
    return m;
  }, [layout]);

  // Citation subtree (ancestors + descendants) + ancestor edges for 思想源流.
  const { subtree, ancestorEdges } = useMemo(() => {
    if (!focus) return { subtree: null as Set<string> | null, ancestorEdges: new Set<string>() };
    const cites = (data.edges || []).filter((e) => e.type === "cites");
    const up = new Set<string>([focus]);
    const down = new Set<string>([focus]);
    let grew = true;
    while (grew) {
      grew = false;
      for (const e of cites) {
        if (down.has(e.source) && !down.has(e.target)) { down.add(e.target); grew = true; }
        if (up.has(e.target) && !up.has(e.source)) { up.add(e.source); grew = true; }
      }
    }
    // Ancestor chain: edges reachable by following citer -> cited from focus.
    const ancEdges = new Set<string>();
    const seen = new Set<string>([focus]);
    const queue = [focus];
    while (queue.length) {
      const cur = queue.shift()!;
      for (const e of cites) {
        if (e.source !== cur) continue;
        ancEdges.add(`${e.source}->${e.target}`);
        if (!seen.has(e.target)) { seen.add(e.target); queue.push(e.target); }
      }
    }
    return { subtree: new Set([...up, ...down]), ancestorEdges: ancEdges };
  }, [focus, data.edges]);

  if (!layout.nodes.length) {
    return <div className="p-4 text-center text-[12px] text-muted">暂无谱系图数据</div>;
  }

  // Auto-fit the frame on both axes (capped upscale), then center.
  const fit = Math.min(
    MAX_UPSCALE,
    (VIEW_W - 12) / Math.max(layout.width, 1),
    (VIEW_H - 12) / Math.max(layout.height, 1),
  );
  const scale = view.k * fit;
  // Centered base translation + user pan offset.
  const tx = (VIEW_W - layout.width * scale) / 2 + view.dx;
  const ty = (VIEW_H - layout.height * scale) / 2 + view.dy;

  // Zoom around the viewport center so the visible region stays put.
  const zoomBy = (factor: number) => {
    setView((v) => {
      const k = Math.min(3, Math.max(0.4, v.k * factor));
      if (k === v.k) return v;
      const s0 = v.k * fit, s1 = k * fit;
      const cx = VIEW_W / 2, cy = VIEW_H / 2;
      const tx0 = (VIEW_W - layout.width * s0) / 2 + v.dx;
      const ty0 = (VIEW_H - layout.height * s0) / 2 + v.dy;
      const tx1 = cx - ((cx - tx0) * s1) / s0;
      const ty1 = cy - ((cy - ty0) * s1) / s0;
      return {
        k,
        dx: tx1 - (VIEW_W - layout.width * s1) / 2,
        dy: ty1 - (VIEW_H - layout.height * s1) / 2,
      };
    });
  };

  // Drag pan (mouse only; the wheel stays on page scroll).
  const onPanStart = (e: React.MouseEvent) => {
    dragRef.current = { startX: e.clientX, startY: e.clientY, x: e.clientX, y: e.clientY, moved: false };
  };
  const onPanMove = (e: React.MouseEvent) => {
    const d = dragRef.current;
    if (!d) return;
    const rect = svgRef.current?.getBoundingClientRect();
    const per = rect ? Math.min(rect.width / VIEW_W, rect.height / VIEW_H) || 1 : 1;
    const dx = (e.clientX - d.x) / per;
    const dy = (e.clientY - d.y) / per;
    d.x = e.clientX;
    d.y = e.clientY;
    if (Math.abs(e.clientX - d.startX) + Math.abs(e.clientY - d.startY) > 4) d.moved = true;
    setView((v) => ({ ...v, dx: v.dx + dx, dy: v.dy + dy }));
  };
  const onPanEnd = () => {
    if (dragRef.current?.moved) {
      // Swallow the click event that follows a real drag, then re-arm.
      suppressClick.current = true;
      setTimeout(() => { suppressClick.current = false; }, 0);
    }
    dragRef.current = null;
  };
  const clickGuard = () => {
    if (suppressClick.current) { suppressClick.current = false; return true; }
    return false;
  };

  const dim = (id: string) => {
    if (!subtree) return false;
    return !subtree.has(id);
  };
  const yearOf = (id: string) => fullById.get(id)?.year ?? 0;
  const years = layout.yearTicks.map((t) => t.year);
  const yMin = years[0] ?? 0, yMax = years[years.length - 1] ?? 1;

  const selectPaper = (id: string) => {
    setSelection({ kind: "paper", id });
    setFocus((f) => (f === id ? null : id));
  };
  const toggleCluster = (cid: number) =>
    setHiddenClusters((prev) => {
      const next = new Set(prev);
      if (next.has(cid)) next.delete(cid); else next.add(cid);
      return next;
    });

  const selPaper = selection?.kind === "paper" ? fullById.get(selection.id) : null;
  const selEdge = selection?.kind === "edge" ? selection : null;
  const selEdgeData = selEdge
    ? drawnEdges.find(
        (e) => e.source === selEdge.source && e.target === selEdge.target && e.type === selEdge.etype,
      ) ?? null
    : null;
  const labelOf = (cid: number) => (clusterLabels && clusterLabels[cid]) || `主题簇 ${cid + 1}`;

  // Hover tooltip payload, in root viewBox coordinates (drawn above the graph).
  const hoverTip = (() => {
    if (!hover) return null;
    const pn = layout.nodes.find((n) => n.id === hover);
    if (pn) {
      const role = pn.role ? ` · ${ROLE_LABEL[pn.role] || ""}` : "";
      return {
        x: pn.x * scale + tx,
        y: pn.y * scale + ty,
        accent: CLUSTER_COLORS[pn.cluster % CLUSTER_COLORS.length],
        lines: [
          truncateToWidth(pn.title || "（无标题）", 262, 10),
          `${pn.year > 0 ? pn.year : "年份未知"} · 被引 ${pn.citation_count} · ${labelOf(pn.cluster)}${role}`,
        ],
      };
    }
    return null;
  })();

  return (
    <div className="overflow-hidden rounded-[7px] border border-border-light bg-surface" style={{ boxShadow: "var(--shadow-md)" }}>
      {/* Filter chips */}
      <div className="flex flex-wrap items-center gap-1.5 border-b border-border-light px-3 py-2">
        {clusterChips.map(([cid, count]) => {
          const hidden = hiddenClusters.has(cid);
          return (
            <button key={cid} onClick={() => toggleCluster(cid)}
              className={`flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[12px] transition-colors ${
                hidden ? "border-border-light text-muted line-through" : "border-border-light text-fg-secondary hover:border-accent/40"}`}>
              <span className="inline-block h-2 w-2 rounded-full"
                style={{ background: CLUSTER_COLORS[cid % CLUSTER_COLORS.length], opacity: hidden ? 0.3 : 1 }} />
              {labelOf(cid)} · {count}
            </button>
          );
        })}
        <button onClick={() => setShowCandidates((s) => !s)}
          className={`rounded-full border border-border-light px-2 py-0.5 text-[12px] transition-colors ${
            showCandidates ? "text-fg-secondary hover:border-accent/40" : "text-muted line-through"}`}>
          候选集
        </button>
        <button onClick={() => setShowSemantic((s) => !s)}
          className={`rounded-full border border-border-light px-2 py-0.5 text-[12px] transition-colors ${
            showSemantic ? "text-fg-secondary hover:border-accent/40" : "text-muted line-through"}`}>
          语义边
        </button>
        <span className="ml-auto hidden text-[12px] text-muted sm:inline">
          悬停看论文信息 · 点击节点看详情 · 点击边看关系 · 拖拽平移 · 右下角缩放
        </span>
        <button onClick={() => setView({ dx: 0, dy: 0, k: 1 })}
          className="rounded-full border border-border-light px-2 py-0.5 text-[12px] text-muted transition-colors hover:border-accent/40 hover:text-fg-secondary">
          复位
        </button>
      </div>

      <div className="relative">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
          className="w-full cursor-grab select-none active:cursor-grabbing"
          style={{ aspectRatio: `${VIEW_W} / ${VIEW_H}` }}
          onMouseDown={onPanStart}
          onMouseMove={onPanMove}
          onMouseUp={onPanEnd}
          onMouseLeave={onPanEnd}
        >
          <defs>
            <marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
              <path d="M 0 0 L 8 4 L 0 8 z" fill="context-stroke" />
            </marker>
          </defs>
          <g transform={`translate(${tx},${ty}) scale(${scale})`}>
            {/* Year axis (equal-spaced ticks) */}
            {layout.yearTicks.map((t) => (
              <g key={t.year}>
                <line x1={t.x} y1={16} x2={t.x} y2={layout.height - 14}
                  stroke="rgb(var(--border-light))" strokeWidth="1" strokeDasharray="2 4" />
                <text x={t.x} y={12} textAnchor="middle" fontSize="10" fill="rgb(var(--muted))" className="tnum">{t.year}</text>
              </g>
            ))}
            {/* Cluster lanes */}
            {layout.lanes.map((lane) => (
              <g key={lane.cluster}>
                <rect x={6} y={lane.y} width={VIEW_W - 12} height={lane.height} rx={10}
                  fill={CLUSTER_COLORS[lane.cluster % CLUSTER_COLORS.length]} opacity={0.05} />
                <text x={14} y={lane.y + 14} fontSize="10" fontWeight={600}
                  fill={CLUSTER_COLORS[lane.cluster % CLUSTER_COLORS.length]} opacity={0.85}>
                  {labelOf(lane.cluster)}（{lane.memberCount}）
                </text>
              </g>
            ))}
            {/* Edges (visible path + fat transparent hit path for clicking) */}
            {drawnEdges.map((e, i) => {
              const s = pointById.get(e.source), t = pointById.get(e.target);
              if (!s || !t) return null;
              if (e.type === "semantic" && !showSemantic) return null;
              const isCite = e.type === "cites";
              const srcCluster = fullById.get(e.source)?.cluster ?? 0;
              const color = isCite ? CLUSTER_COLORS[srcCluster % CLUSTER_COLORS.length] : "#b5b0a2";
              const key = `${e.source}->${e.target}`;
              const inFlow = focus != null && ancestorEdges.has(key);
              const faded = subtree != null && !inFlow &&
                !(subtree.has(e.source) || subtree.has(e.target));
              // 思想源流: ancestor edges thicken with age (older target = thicker).
              const age = yMax > yMin ? (yMax - yearOf(e.target)) / (yMax - yMin) : 0;
              const width = inFlow ? 1.6 + 2.6 * age : isCite ? 1.3 : 1;
              const isSel = selEdge != null && selEdge.source === e.source &&
                selEdge.target === e.target && selEdge.etype === e.type;
              const d = edgePath(s, t);
              return (
                <g key={i}>
                  <path d={d} fill="none" pointerEvents="none"
                    stroke={color} strokeWidth={isSel ? width + 1.4 : width}
                    strokeDasharray={isCite ? undefined : "4 3"}
                    markerEnd={isCite ? "url(#arrow)" : undefined}
                    opacity={isSel ? 1 : faded ? 0.05 : inFlow ? 0.9 : isCite ? 0.5 : 0.3} />
                  {!faded && (
                    <path d={d} fill="none" stroke="transparent" strokeWidth={12}
                      pointerEvents="stroke" style={{ cursor: "pointer" }}
                      onClick={(ev) => {
                        ev.stopPropagation();
                        if (clickGuard()) return;
                        setSelection(isSel ? null : { kind: "edge", source: e.source, target: e.target, etype: e.type });
                      }}>
                      <title>{isCite ? "引用关系 · 点击查看详情" : "语义相似 · 点击查看详情"}</title>
                    </path>
                  )}
                </g>
              );
            })}
            {/* Paper nodes */}
            {layout.nodes.map((n) => (
              <PaperNode key={n.id} n={n} focused={focus === n.id} faded={dim(n.id)}
                selected={selection?.kind === "paper" && selection.id === n.id}
                onClick={() => { if (!clickGuard()) selectPaper(n.id); }}
                onHover={(id) => setHover(id)} />
            ))}
          </g>
          {hoverTip && <NodeTip tip={hoverTip} />}
        </svg>

        {/* Zoom controls (bottom-right, map-style) */}
        <div className="absolute bottom-3 right-3 flex flex-col overflow-hidden rounded-[7px] border border-border-light bg-surface shadow-md">
          <button onClick={() => zoomBy(1.25)} title="放大" aria-label="放大"
            className="flex h-8 w-8 items-center justify-center text-fg-secondary transition-colors hover:bg-bg hover:text-accent">
            <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
              <path d="M6.5 2v9M2 6.5h9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          </button>
          <button onClick={() => zoomBy(0.8)} title="缩小" aria-label="缩小"
            className="flex h-8 w-8 items-center justify-center border-t border-border-light text-fg-secondary transition-colors hover:bg-bg hover:text-accent">
            <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
              <path d="M2 6.5h9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          </button>
        </div>
      </div>

      {/* Legend row */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-border-light px-3 py-1.5 text-[12px] text-muted">
        <span className="flex items-center gap-1">
          <svg width="14" height="14"><circle cx="7" cy="7" r="4" fill="none" stroke="#476d3a" strokeWidth="1.4" /><circle cx="7" cy="7" r="6.2" fill="#476d3a" opacity="0.18" /></svg>
          奠基
        </span>
        <span className="flex items-center gap-1">
          <svg width="14" height="14"><circle cx="7" cy="7" r="4" fill="#4a628a" /></svg>
          核心集（桥梁为其中枢纽）
        </span>
        <span className="flex items-center gap-1">
          <svg width="14" height="14"><circle cx="7" cy="7" r="4" fill="none" stroke="#8a6d3b" strokeWidth="1.6" /></svg>
          候选集
        </span>
        <span className="flex items-center gap-1">
          <svg width="22" height="8"><line x1="0" y1="4" x2="18" y2="4" stroke="#8a8577" strokeWidth="2.6" markerEnd="url(#arrow)" /></svg>
          思想源流（越老越粗）
        </span>
        <span className="flex items-center gap-1">
          <svg width="18" height="8"><line x1="0" y1="4" x2="16" y2="4" stroke="#b5b0a2" strokeWidth="1.2" strokeDasharray="3 2" /></svg>
          语义相似
        </span>
        <span className="ml-auto text-muted">
          核心集 <b className="tnum">{layout.stats.core}</b> · 候选 <b className="tnum">{layout.stats.candidates}</b>
          · 引用边 <b className="tnum">{layout.stats.citeEdges}</b> · 语义边 <b className="tnum">{layout.stats.semanticEdges}</b>
          · 奠基 <b className="tnum">{layout.stats.foundational}</b> · 桥梁 <b className="tnum">{layout.stats.bridge}</b>
        </span>
      </div>

      {/* Detail panels */}
      {selPaper && (
        <PaperDetail
          paper={selPaper}
          edges={data.edges || []}
          fullById={fullById}
          clusterLabel={labelOf(selPaper.cluster)}
          onJump={selectPaper}
          onClose={() => { setSelection(null); setFocus(null); }}
          onDeepAsk={(p) => setComposerDraft(`深问这篇论文 [${p.id}]《${p.title}》：`)}
        />
      )}
      {selEdge && selEdgeData && (
        <EdgeDetail
          edge={selEdge}
          weight={selEdgeData.weight}
          fullById={fullById}
          labelOf={labelOf}
          onJumpPaper={selectPaper}
          onClose={() => setSelection(null)}
        />
      )}
    </div>
  );
}

function NodeTip({ tip }: { tip: { x: number; y: number; accent: string; lines: string[]; lineColors?: (string | null)[] } }) {
  const fs = 10;
  const w = Math.min(300, Math.max(...tip.lines.map((l) => estTextWidth(l, fs))) + 18);
  const h = tip.lines.length * 14 + 10;
  const tx = Math.min(Math.max(tip.x - w / 2, 4), VIEW_W - w - 4);
  const above = tip.y - h - 14 >= 6;
  const ty = above ? tip.y - h - 14 : tip.y + 16;
  return (
    <g pointerEvents="none">
      <rect x={tx} y={ty} width={w} height={h} rx={6}
        fill="rgb(var(--surface))" stroke={tip.accent} strokeWidth={1} opacity={0.98} />
      {tip.lines.map((l, i) => (
        <text key={i} x={tx + 9} y={ty + 16 + i * 14} fontSize={fs}
          fontWeight={i === 0 ? 600 : 400}
          fill={i === 0 ? "rgb(var(--fg))" : tip.lineColors?.[i] || "rgb(var(--muted))"}>
          {l}
        </text>
      ))}
    </g>
  );
}

function PaperNode({ n, focused, faded, selected, onClick, onHover }: {
  n: PositionedNode; focused: boolean; faded: boolean; selected: boolean;
  onClick: () => void; onHover: (id: string | null) => void;
}) {
  const color = CLUSTER_COLORS[n.cluster % CLUSTER_COLORS.length];
  const isCore = n.layer === "core";
  return (
    <g transform={`translate(${n.x},${n.y})`} opacity={faded ? 0.15 : 1}
      style={{ cursor: "pointer" }}
      onMouseEnter={() => onHover(n.id)}
      onMouseLeave={() => onHover(null)}
      onClick={(e) => { e.stopPropagation(); onClick(); }}>
      {n.role === "foundational" && <circle r={n.r + 5} fill={color} opacity={0.18} />}
      <circle r={n.r} fill={isCore ? color : "rgb(var(--surface))"} stroke={color}
        strokeWidth={selected ? 2.4 : isCore ? 1 : 1.6} />
      {focused && <circle r={n.r + 3} fill="none" stroke={color} strokeWidth={1.2} strokeDasharray="2 2" />}
    </g>
  );
}

function EdgeDetail({ edge, weight, fullById, labelOf, onJumpPaper, onClose }: {
  edge: { source: string; target: string; etype: string };
  weight: number;
  fullById: Map<string, GraphNode>;
  labelOf: (cid: number) => string;
  onJumpPaper: (id: string) => void;
  onClose: () => void;
}) {
  const isCite = edge.etype === "cites";
  const a = fullById.get(edge.source);
  const b = fullById.get(edge.target);

  const card = (p: GraphNode | undefined) => {
    if (!p) return <p className="text-[12px] text-muted">端点不在当前视图</p>;
    return (
      <button onClick={() => onJumpPaper(p.id)} title={p.title}
        className="w-full rounded-[7px] border border-border-light px-2.5 py-1.5 text-left transition-colors hover:border-accent/40">
        <p className="truncate text-[12px] font-medium text-fg">{p.title}</p>
        <p className="mt-0.5 text-[12px] text-muted">
          <span className="tnum">{p.year > 0 ? p.year : "????"}</span>
          {` · 被引 `}<span className="tnum">{p.citation_count}</span>
          {` · `}{labelOf(p.cluster)}
          {p.role ? ` · ${ROLE_LABEL[p.role] || ""}` : ""}
        </p>
      </button>
    );
  };

  return (
    <div className="h-[190px] overflow-y-auto border-t border-border-light px-4 py-3">
      <div className="flex items-start justify-between gap-3">
        <p className="text-[13px] font-semibold text-fg">
          {isCite ? "引用关系" : "语义相似"}
          {!isCite && weight > 0 && (
            <span className="tnum ml-2 text-[12px] font-normal text-muted">相似度 {weight.toFixed(2)}</span>
          )}
        </p>
        <button onClick={onClose} className="px-1.5 text-[13px] text-muted hover:text-fg" title="关闭">×</button>
      </div>
      <p className="mt-1 text-[12px] text-muted">
        {isCite
          ? "左侧论文引用了右侧论文（思想由被引方流向引用方）。点击任一卡片可跳到对应论文。"
          : "两篇论文研究内容相近（按摘要嵌入向量相似度连边，无引用方向）。点击任一卡片可跳到对应论文。"}
      </p>
      <div className="mt-2 grid grid-cols-[1fr_auto_1fr] items-center gap-2">
        {card(a)}
        <div className="flex flex-col items-center px-1 text-muted">
          {isCite ? (
            <svg width="34" height="14" viewBox="0 0 34 14">
              <line x1="2" y1="7" x2="26" y2="7" stroke="currentColor" strokeWidth="1.4" />
              <path d="M26 3 L33 7 L26 11 z" fill="currentColor" />
            </svg>
          ) : (
            <svg width="30" height="14" viewBox="0 0 30 14">
              <line x1="2" y1="7" x2="28" y2="7" stroke="currentColor" strokeWidth="1.4" strokeDasharray="3 2" />
            </svg>
          )}
          <span className="mt-0.5 text-[9px]">{isCite ? "引用了" : "相近"}</span>
        </div>
        {card(b)}
      </div>
    </div>
  );
}

function PaperDetail({ paper, edges, fullById, clusterLabel, onJump, onClose, onDeepAsk }: {
  paper: GraphNode;
  edges: { source: string; target: string; type: string }[];
  fullById: Map<string, GraphNode>;
  clusterLabel: string;
  onJump: (id: string) => void;
  onClose: () => void;
  onDeepAsk: (p: GraphNode) => void;
}) {
  // In-library citation relations only (both endpoints resolved to real nodes).
  const citesOut = edges.filter((e) => e.type === "cites" && e.source === paper.id)
    .map((e) => fullById.get(e.target)).filter(Boolean) as GraphNode[];
  const citedBy = edges.filter((e) => e.type === "cites" && e.target === paper.id)
    .map((e) => fullById.get(e.source)).filter(Boolean) as GraphNode[];

  const relList = (list: GraphNode[], emptyText: string) =>
    list.length ? (
      <ul className="space-y-0.5">
        {list.map((p) => (
          <li key={p.id}>
            <button onClick={() => onJump(p.id)}
              className="w-full truncate text-left text-[12px] text-fg-secondary transition-colors hover:text-accent"
              title={p.title}>
              <span className="tnum text-muted">{p.year > 0 ? p.year : "????"}</span> · {p.title}
            </button>
          </li>
        ))}
      </ul>
    ) : <p className="text-[12px] text-muted">{emptyText}</p>;

  return (
    <div className="h-[190px] overflow-y-auto border-t border-border-light px-4 py-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[13px] font-semibold leading-snug text-fg">{paper.title}</p>
          <p className="mt-0.5 text-[12px] text-muted">
            {(paper.authors || []).slice(0, 3).join(", ") || "作者未知"}
            {paper.year > 0 && ` · ${paper.year}`}
            {paper.venue && ` · ${paper.venue}`}
            {` · 被引 `}<span className="tnum">{paper.citation_count}</span>
            {` · `}{clusterLabel}
            {paper.role === "foundational" && <span className="badge-accent2 badge ml-1.5">奠基</span>}
            {paper.role === "bridge" && <span className="badge-accent badge ml-1.5">桥梁</span>}
            {paper.layer !== "core" && <span className="badge ml-1.5 border border-border-light text-muted">候选</span>}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {paper.url && (
            <a href={paper.url} target="_blank" rel="noreferrer"
              title="出版页面链接"
              className="rounded-[7px] border border-border-light px-2 py-1 text-[12px] text-fg-secondary transition-colors hover:border-accent/40 hover:text-accent">
              原文链接
            </a>
          )}
          <button onClick={() => onDeepAsk(paper)}
            className="rounded-[7px] bg-accent px-2 py-1 text-[12px] text-white transition-colors hover:bg-accent-hover">
            深问这篇
          </button>
          <button onClick={onClose} className="px-1.5 text-[13px] text-muted hover:text-fg" title="关闭">×</button>
        </div>
      </div>
      {paper.abstract && (
        <p className="mt-1.5 line-clamp-2 text-[12px] leading-relaxed text-muted">{paper.abstract}…</p>
      )}
      <div className="mt-2 grid grid-cols-2 gap-4">
        <div>
          <p className="mb-0.5 text-[12px] font-semibold uppercase tracking-wide text-muted">
            它引用的（{citesOut.length}）</p>
          {relList(citesOut, "库内无被它引用的论文")}
        </div>
        <div>
          <p className="mb-0.5 text-[12px] font-semibold uppercase tracking-wide text-muted">
            被引用的（{citedBy.length}）</p>
          {relList(citedBy, "库内暂无引用它的论文")}
        </div>
      </div>
    </div>
  );
}
