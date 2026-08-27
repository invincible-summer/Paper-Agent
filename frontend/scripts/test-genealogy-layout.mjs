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

test("every bucket member is positioned — nothing collapses into aggregates", () => {
  const nodes = Array.from({ length: 6 }, (_, i) =>
    N(`p${i}`, { year: 2020, citation_count: i * 100 }));
  const r = layoutGenealogy({ nodes, edges: [] });
  assert.equal(r.nodes.length, 6, "all members shown individually");
  // stacked at the same x (same year) on distinct rows, ranked by citations
  // desc so the most-cited paper sits on top
  const xs = new Set(r.nodes.map((n) => n.x));
  assert.equal(xs.size, 1);
  const rows = r.nodes.map((n) => n.y).sort((a, b) => a - b);
  for (let i = 1; i < rows.length; i++) {
    assert.ok(rows[i] - rows[i - 1] > 0, "distinct rows, no overlap");
  }
  const top = r.nodes.reduce((a, b) => (a.y < b.y ? a : b));
  assert.equal(top.id, "p5", "highest-cited paper on the first row");
});

test("lane height grows with the tallest bucket (min one row)", () => {
  const one = layoutGenealogy({ nodes: [N("a", { year: 2020 }), N("b", { year: 2021 })], edges: [] });
  const dense = layoutGenealogy({
    nodes: Array.from({ length: 5 }, (_, i) => N(`p${i}`, { year: 2020, citation_count: i })),
    edges: [],
  });
  assert.equal(one.lanes[0].height, 24 + 1 * 44, "single-row lane keeps the minimum height");
  assert.equal(dense.lanes[0].height, 24 + 5 * 44, "lane grows to fit its tallest bucket");
  assert.ok(dense.height > one.height);
});

test("lanes sorted by cluster size desc", () => {
  const nodes = [N("a", { cluster: 0 }), N("b", { cluster: 1 }), N("c", { cluster: 1 }), N("d", { cluster: 1 })];
  const r = layoutGenealogy({ nodes, edges: [] });
  assert.equal(r.lanes[0].cluster, 1, "biggest cluster on top");
  assert.equal(r.lanes[1].cluster, 0);
  assert.ok(r.lanes[1].y > r.lanes[0].y);
});

test("resolveEdges drops self-loops and duplicate edges", () => {
  const edges = [
    { source: "x", target: "p1", type: "cites", weight: 1 },
    { source: "x", target: "p1", type: "cites", weight: 1 }, // exact duplicate
    { source: "p1", target: "p1", type: "cites", weight: 1 }, // self-loop
    { source: "x", target: "p1", type: "semantic", weight: 0.7 }, // different type: kept
  ];
  const out = resolveEdges(edges);
  assert.equal(out.length, 2);
  assert.deepEqual(out.map((e) => e.type).sort(), ["cites", "semantic"]);
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
