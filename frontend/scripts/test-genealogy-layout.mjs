// Unit tests for frontend/lib/genealogy-layout.ts (pure layout function).
// Run: node --experimental-strip-types --test scripts/test-genealogy-layout.mjs
import test from "node:test";
import assert from "node:assert/strict";
import {
  layoutGenealogy,
  resolveEdges,
  graphStats,
  nodeRadius,
} from "../lib/genealogy-layout.ts";

const N = (id, over = {}) => ({
  id, title: `Paper ${id}`, year: 2020, citation_count: 0,
  cluster: 0, role: "", layer: "core", authors: [], url: "",
  abstract: "", venue: "", ...over,
});

test("empty input -> empty layout with zero height", () => {
  const r = layoutGenealogy({ nodes: [], edges: [] });
  assert.equal(r.nodes.length, 0);
  assert.equal(r.height, 0);
  assert.deepEqual(r.lanes, []);
});

test("deterministic: same input -> identical coordinates", () => {
  const data = {
    nodes: [N("a", { year: 2019 }), N("b", { year: 2021, citation_count: 60 }), N("c", { year: 2021 })],
    edges: [],
  };
  const r1 = layoutGenealogy(data);
  const r2 = layoutGenealogy(JSON.parse(JSON.stringify(data)));
  assert.deepEqual(r1.nodes.map((n) => [n.id, n.x, n.y, n.r]),
                   r2.nodes.map((n) => [n.id, n.x, n.y, n.r]));
});

test("equal-spaced year axis: gaps between ticks are uniform", () => {
  const data = {
    nodes: [N("a", { year: 2001 }), N("b", { year: 2002 }), N("c", { year: 2026 })],
    edges: [],
  };
  const r = layoutGenealogy(data);
  assert.equal(r.yearTicks.length, 3);
  const gaps = r.yearTicks.slice(1).map((t, i) => t.x - r.yearTicks[i].x);
  assert.ok(Math.abs(gaps[0] - gaps[1]) < 1e-9, "tick spacing must be equal");
});

test("bucket overflow collapses into a +N aggregate with alias map", () => {
  const nodes = Array.from({ length: 6 }, (_, i) =>
    N(`p${i}`, { year: 2020, citation_count: i * 100 }));
  const r = layoutGenealogy({ nodes, edges: [] }, { bucketTopN: 3 });
  assert.equal(r.nodes.length, 3, "only top-3 per bucket shown");
  assert.equal(r.aggregates.length, 1);
  const agg = r.aggregates[0];
  assert.equal(agg.count, 3);
  assert.equal(agg.members.length, 3);
  // top-3 by citations kept visible; the rest aliased to the aggregate
  const shownIds = new Set(r.nodes.map((n) => n.id));
  assert.ok(shownIds.has("p5") && shownIds.has("p4") && shownIds.has("p3"));
  for (const hidden of ["p0", "p1", "p2"]) assert.equal(r.aliasOf[hidden], agg.id);
});

test("fixed lane height regardless of bucket overflow (stable canvas size)", () => {
  const small = layoutGenealogy({ nodes: [N("a")], edges: [] });
  const big = layoutGenealogy({
    nodes: Array.from({ length: 30 }, (_, i) => N(`p${i}`, { year: 2020, citation_count: i })),
    edges: [],
  });
  assert.equal(small.lanes[0].height, big.lanes[0].height, "lane height is fixed");
  assert.equal(small.lanes.length, big.lanes.length);
});

test("lanes sorted by cluster size desc", () => {
  const nodes = [N("a", { cluster: 0 }), N("b", { cluster: 1 }), N("c", { cluster: 1 }), N("d", { cluster: 1 })];
  const r = layoutGenealogy({ nodes, edges: [] });
  assert.equal(r.lanes[0].cluster, 1, "biggest cluster on top");
  assert.equal(r.lanes[1].cluster, 0);
  assert.ok(r.lanes[1].y > r.lanes[0].y);
});

test("resolveEdges re-attaches hidden endpoints and dedupes", () => {
  const edges = [
    { source: "x", target: "p1", type: "cites", weight: 1 },
    { source: "x", target: "p2", type: "cites", weight: 1 },
    { source: "p1", target: "p2", type: "cites", weight: 1 }, // self-loop via agg
  ];
  const aliasOf = { p1: "agg::0::2020", p2: "agg::0::2020" };
  const out = resolveEdges(edges, aliasOf);
  assert.equal(out.length, 1, "dupes and self-loops removed");
  assert.equal(out[0].target, "agg::0::2020");
});

test("nodeRadius has exactly three tiers", () => {
  assert.equal(nodeRadius(0), 7);
  assert.equal(nodeRadius(49), 7);
  assert.equal(nodeRadius(50), 10);
  assert.equal(nodeRadius(499), 10);
  assert.equal(nodeRadius(500), 13);
});

test("graphStats counts layers, edge types, roles", () => {
  const stats = graphStats({
    nodes: [
      N("a", { layer: "core", role: "foundational" }),
      N("b", { layer: "core", role: "bridge" }),
      N("c", { layer: "candidate" }),
    ],
    edges: [
      { source: "a", target: "b", type: "cites", weight: 1 },
      { source: "b", target: "c", type: "semantic", weight: 0.6 },
    ],
  });
  assert.deepEqual(stats, {
    core: 2, candidates: 1, citeEdges: 1, semanticEdges: 1,
    foundational: 1, bridge: 1,
  });
});

test("unknown-year nodes fall back to the first tick", () => {
  const r = layoutGenealogy({ nodes: [N("a", { year: 0 }), N("b", { year: 2015 })], edges: [] });
  const a = r.nodes.find((n) => n.id === "a");
  assert.equal(a.x, r.yearTicks[0].x);
});
