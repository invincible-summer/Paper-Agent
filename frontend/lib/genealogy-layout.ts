// Deterministic layout for the genealogy graph (论文谱系图 v2).
//
// Pure function: same input -> same output, no randomness, no DOM. This makes
// the layout unit-testable in plain Node and keeps the rendered graph stable
// in size — lanes have a fixed row budget, oversized (cluster, year) buckets
// collapse into "+N" aggregate nodes, and years are spaced by ordinal (equal
// gaps) instead of real calendar distance so sparse decades never stretch the
// canvas.

import type { GenealogyGraph, GraphNode, GraphEdge } from "./types";

export interface LayoutOptions {
  width?: number;      // content width of the canvas (px)
  bucketTopN?: number; // members shown per (cluster, year) bucket before "+N"
}

export interface PositionedNode extends GraphNode {
  x: number;
  y: number;
  r: number;
}

export interface AggregateNode {
  id: string;          // "agg::{cluster}::{year}"
  kind: "aggregate";
  cluster: number;
  year: number;
  count: number;
  members: { id: string; title: string; year: number; citation_count: number }[];
  x: number;
  y: number;
}

export interface Lane {
  cluster: number;
  y: number;
  height: number;
  memberCount: number;
  shownCount: number;  // excludes aggregated members
}

export interface GraphStats {
  core: number;
  candidates: number;
  citeEdges: number;
  semanticEdges: number;
  foundational: number;
  bridge: number;
}

export interface LayoutResult {
  nodes: PositionedNode[];
  aggregates: AggregateNode[];
  lanes: Lane[];
  yearTicks: { year: number; x: number }[];
  // hidden (aggregated) node id -> its aggregate node id, so edges can be
  // re-attached instead of dropped.
  aliasOf: Record<string, string>;
  width: number;
  height: number;
  stats: GraphStats;
}

const PAD_L = 20;
const PAD_R = 28;
const PAD_T = 36;   // room for the year axis
const PAD_B = 24;
const ROW_GAP = 44; // vertical distance between node rows inside a lane
const LANE_LABEL = 24;

export function nodeRadius(citationCount: number): number {
  // Three fixed tiers (log-ish buckets) — visually calm, no continuous scale.
  if (citationCount >= 500) return 13;
  if (citationCount >= 50) return 10;
  return 7;
}

export function graphStats(data: GenealogyGraph): GraphStats {
  const nodes = data.nodes || [];
  const edges = data.edges || [];
  return {
    core: nodes.filter((n) => n.layer === "core").length,
    candidates: nodes.filter((n) => n.layer !== "core").length,
    citeEdges: edges.filter((e) => e.type === "cites").length,
    semanticEdges: edges.filter((e) => e.type === "semantic").length,
    foundational: nodes.filter((n) => n.role === "foundational").length,
    bridge: nodes.filter((n) => n.role === "bridge").length,
  };
}

export function layoutGenealogy(
  data: GenealogyGraph,
  opts: LayoutOptions = {},
): LayoutResult {
  const width = opts.width ?? 920;
  const bucketTopN = opts.bucketTopN ?? 3;
  const nodes = data.nodes || [];
  const stats = graphStats(data);
  const empty: LayoutResult = {
    nodes: [], aggregates: [], lanes: [], yearTicks: [],
    aliasOf: {}, width, height: 0, stats,
  };
  if (!nodes.length) return empty;

  // Equal-spaced year axis over the distinct years present.
  const years = Array.from(
    new Set(nodes.map((n) => n.year).filter((y) => y > 0)),
  ).sort((a, b) => a - b);
  const span = Math.max(years.length - 1, 1);
  const step = (width - PAD_L - PAD_R) / Math.max(years.length, 1);
  const yearTicks = years.map((year, i) => ({
    year,
    x: PAD_L + step / 2 + (i * (width - PAD_L - PAD_R - step)) / span,
  }));
  const xOfYear = (year: number): number => {
    if (year <= 0 || !yearTicks.length) return yearTicks[0]?.x ?? PAD_L + step / 2;
    const idx = years.indexOf(year);
    return yearTicks[idx >= 0 ? idx : 0].x;
  };

  // Lanes ordered by cluster size desc (biggest topic on top).
  const byCluster = new Map<number, GraphNode[]>();
  for (const n of nodes) {
    const arr = byCluster.get(n.cluster) || [];
    arr.push(n);
    byCluster.set(n.cluster, arr);
  }
  const clusterIds = Array.from(byCluster.keys()).sort(
    (a, b) => byCluster.get(b)!.length - byCluster.get(a)!.length || a - b,
  );

  const positioned: PositionedNode[] = [];
  const aggregates: AggregateNode[] = [];
  const lanes: Lane[] = [];
  const aliasOf: Record<string, string> = {};
  let cursor = PAD_T;

  for (const cid of clusterIds) {
    const members = byCluster.get(cid)!;
    const laneHeight = LANE_LABEL + (bucketTopN + 1) * ROW_GAP;
    lanes.push({
      cluster: cid, y: cursor, height: laneHeight,
      memberCount: members.length, shownCount: 0,
    });

    // Bucket by year; inside a bucket rank by citations (id tie-break).
    const buckets = new Map<number, GraphNode[]>();
    for (const m of members) {
      const y = m.year > 0 ? m.year : 0;
      const arr = buckets.get(y) || [];
      arr.push(m);
      buckets.set(y, arr);
    }
    let shown = 0;
    for (const [year, bucket] of Array.from(buckets.entries()).sort((a, b) => a[0] - b[0])) {
      const ranked = [...bucket].sort(
        (a, b) => (b.citation_count || 0) - (a.citation_count || 0) || a.id.localeCompare(b.id),
      );
      const x = xOfYear(year);
      ranked.slice(0, bucketTopN).forEach((n, row) => {
        positioned.push({
          ...n, x,
          y: cursor + LANE_LABEL + ROW_GAP / 2 + row * ROW_GAP,
          r: nodeRadius(n.citation_count || 0),
        });
        shown += 1;
      });
      const rest = ranked.slice(bucketTopN);
      if (rest.length) {
        const aggId = `agg::${cid}::${year}`;
        aggregates.push({
          id: aggId, kind: "aggregate", cluster: cid, year,
          count: rest.length,
          members: rest.map((m) => ({
            id: m.id, title: m.title, year: m.year, citation_count: m.citation_count,
          })),
          x,
          y: cursor + LANE_LABEL + ROW_GAP / 2 + bucketTopN * ROW_GAP,
        });
        for (const m of rest) aliasOf[m.id] = aggId;
      }
    }
    lanes[lanes.length - 1].shownCount = shown;
    cursor += laneHeight;
  }

  return {
    nodes: positioned, aggregates, lanes, yearTicks, aliasOf,
    width, height: cursor + PAD_B, stats,
  };
}

/** Resolve an edge endpoint through the aggregate alias map. */
export function resolveEndpoint(id: string, aliasOf: Record<string, string>): string {
  return aliasOf[id] || id;
}

/** Dedupe edges after aggregate aliasing (several hidden members may map to
 * the same aggregate, producing identical drawn edges). */
export function resolveEdges(
  edges: GraphEdge[],
  aliasOf: Record<string, string>,
): GraphEdge[] {
  const seen = new Set<string>();
  const out: GraphEdge[] = [];
  for (const e of edges) {
    const s = resolveEndpoint(e.source, aliasOf);
    const t = resolveEndpoint(e.target, aliasOf);
    if (s === t) continue; // self-loop through an aggregate
    const key = `${s}->${t}:${e.type}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ ...e, source: s, target: t });
  }
  return out;
}
