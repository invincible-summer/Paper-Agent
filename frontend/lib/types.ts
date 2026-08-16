// Shared types for the chat-only frontend.

export interface Paper {
  id: string;
  title: string;
  authors: string[];
  year: number | null;
  venue: string;
  doi: string | null;
  source: string;
  citation_count: number;
  abstract: string;
  pdf_url: string | null;
  keywords: string[];
  urls: Record<string, string>;
  relevance_score: number;
  layer: string;
  fulltext_status: string;   // available | unavailable | unknown（已验证状态）
}

export interface SubDirection {
  name: string;
  queries_en: string[];
  queries_zh: string[];
}

export interface CandidatePaper {
  id: string;
  title: string;
  year: number | null;
  citation_count: number;
  source: string;
  urls: Record<string, string>;
  pdf_url: string | null;
  fulltext_status: string;   // available | unavailable | unknown
}

export interface MapCluster {
  id: number;
  label: string;
  overview: string;
  paper_ids: string[];
  papers: { id: string; title: string; year: number | null; citation_count: number }[];
}

export interface MapTimelineEntry {
  year: number;
  papers: { id: string; title: string; citation_count: number }[];
}

export interface GraphNode {
  id: string;
  title: string;
  year: number;
  citation_count: number;
  cluster: number;
  role: string;            // "foundational" | "bridge" | ""
  layer: string;           // "core" | "candidate"
  authors: string[];
  url: string;
  abstract: string;        // first ~200 chars, for the detail panel
  venue: string;
  fulltext: boolean;         // 已实际下载并解析验证的全文
  fulltext_status: string;   // available | unavailable | unknown
}

export interface GraphEdge {
  source: string;
  target: string;
  type: string;            // "cites" | "semantic"
  weight: number;
}

export interface GenealogyGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface ResearchMapData {
  clusters: MapCluster[];
  timeline: MapTimelineEntry[];
  landscape: string;
  graph: GenealogyGraph;
}

export interface ReadingPathStep {
  paper_id: string;
  title: string;
  role: string;            // 奠基 / 桥梁 / 前沿
  reason: string;
}

export interface HealthResponse {
  status: string;
  app: string;
  env: string;
  api_configured: boolean;
}
