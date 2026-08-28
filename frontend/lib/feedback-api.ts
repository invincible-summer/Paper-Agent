import { authHeaders } from "@/lib/auth";

const BASE = "/api/v1";

export const FEEDBACK_CATEGORIES = ["问题报告", "功能建议", "其他"] as const;
export type FeedbackCategory = (typeof FEEDBACK_CATEGORIES)[number];

export const FEEDBACK_CONTENT_MAX = 4000;
export const FEEDBACK_CONTACT_MAX = 120;

export async function submitFeedback(input: {
  category: FeedbackCategory;
  content: string;
  contact?: string;
}): Promise<void> {
  const res = await fetch(`${BASE}/feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({
      category: input.category,
      content: input.content,
      contact: input.contact || "",
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data?.detail || `提交失败（${res.status}）`);
  }
}
