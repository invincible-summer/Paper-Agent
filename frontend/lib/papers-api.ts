import { authHeaders } from "./auth";

// Downloaded-paper collection: network-paper metadata aggregated from the
// user's sessions (GET /api/v1/papers). Network papers never carry local
// full text — entries link out to the publisher page.

export interface CollectedPaper {
  id: string;
  title: string;
  authors: string[];
  year: number | null;
  venue: string;
  doi: string;
  source: string;
  citation_count: number | null;
  abstract: string;
  keywords: string[];
  urls: string[];
  sessions: { filename: string; title: string }[];
  in_review: boolean;
}

export async function listMyPapers(): Promise<CollectedPaper[]> {
  const response = await fetch("/api/v1/papers", { headers: authHeaders() });
  if (!response.ok) throw new Error(`无法读取论文集（${response.status}）`);
  const data = await response.json();
  return (data.papers || []) as CollectedPaper[];
}
