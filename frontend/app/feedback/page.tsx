"use client";

import { useState } from "react";
import { MessageSquarePlus, Send, Loader2, ArrowLeft, CheckCircle2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { Nav } from "@/components/Nav";
import {
  FEEDBACK_CATEGORIES, FEEDBACK_CONTACT_MAX, FEEDBACK_CONTENT_MAX,
  submitFeedback, type FeedbackCategory,
} from "@/lib/feedback-api";

export default function FeedbackPage() {
  const router = useRouter();
  const [category, setCategory] = useState<FeedbackCategory>("问题报告");
  const [content, setContent] = useState("");
  const [contact, setContact] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  const contentLeft = FEEDBACK_CONTENT_MAX - content.length;

  const onSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    const trimmed = content.trim();
    if (!trimmed) {
      setError("请填写反馈内容");
      return;
    }
    setBusy(true); setError("");
    try {
      await submitFeedback({ category, content: trimmed, contact: contact.trim() });
      setDone(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "提交失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen bg-bg text-fg">
      <Nav />
      <main className="mx-auto max-w-3xl px-4 py-8 sm:px-8">
        <div className="mb-6 flex items-start gap-3">
          <button
            type="button"
            onClick={() => router.push("/chat")}
            aria-label="返回对话"
            title="返回对话"
            className="mt-1 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border-light text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg"
          >
            <ArrowLeft className="h-4 w-4" />
          </button>
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-accent-soft/50 text-accent">
            <MessageSquarePlus className="h-5 w-5" />
          </div>
          <div className="min-w-0">
            <h1 className="text-2xl font-bold">意见反馈</h1>
            <p className="mt-1 text-sm text-muted">
              问题报告与功能建议会直接送达管理员，帮助我们持续改进
            </p>
          </div>
        </div>

        {done ? (
          <section className="rounded-2xl border border-border-light bg-surface p-8 text-center shadow-sm">
            <CheckCircle2 className="mx-auto mb-3 h-10 w-10 text-success" />
            <h2 className="text-lg font-semibold">感谢你的反馈！</h2>
            <p className="mx-auto mt-2 max-w-md text-sm text-muted">
              我们已收到你的{category === "功能建议" ? "建议" : "反馈"}，管理员会在后台查看处理。
            </p>
            <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
              <button
                type="button"
                onClick={() => { setDone(false); setContent(""); setContact(""); setCategory("问题报告"); }}
                className="flex h-9 items-center gap-1.5 rounded-lg border border-border-light px-4 text-sm text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg"
              >
                再写一条
              </button>
              <button
                type="button"
                onClick={() => router.push("/chat")}
                className="flex h-9 items-center rounded-lg bg-accent px-5 text-sm font-medium text-white transition-colors hover:bg-accent-hover"
              >
                返回对话
              </button>
            </div>
          </section>
        ) : (
          <form onSubmit={onSubmit} className="space-y-5 rounded-2xl border border-border-light bg-surface p-6 shadow-sm">
            {error && (
              <p className="rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-sm text-warning">{error}</p>
            )}

            <div>
              <span className="mb-2 block text-sm font-medium">反馈类型</span>
              <div className="flex flex-wrap gap-2">
                {FEEDBACK_CATEGORIES.map((item) => (
                  <button
                    key={item} type="button" onClick={() => setCategory(item)}
                    aria-pressed={category === item}
                    className={`h-9 rounded-lg border px-4 text-sm transition-colors ${
                      category === item
                        ? "border-accent bg-accent-soft/70 font-medium text-accent"
                        : "border-border-light text-fg-secondary hover:bg-surface-hover hover:text-fg"
                    }`}
                  >
                    {item}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <label htmlFor="feedback-content" className="mb-2 block text-sm font-medium">
                反馈内容 <span className="text-error">*</span>
              </label>
              <textarea
                id="feedback-content"
                value={content}
                onChange={(event) => setContent(event.target.value.slice(0, FEEDBACK_CONTENT_MAX))}
                placeholder="请描述遇到的问题或想要的功能，例如操作步骤、期望效果等"
                rows={7}
                required
                className="w-full resize-y rounded-xl border border-border-light bg-bg p-3.5 text-sm leading-6 text-fg outline-none transition-colors focus:border-accent/60 focus:ring-2 focus:ring-accent/15"
              />
              <p className={`mt-1.5 text-right text-xs ${contentLeft < 200 ? "text-warning" : "text-muted"}`}>
                还可输入 {contentLeft} 字
              </p>
            </div>

            <div>
              <label htmlFor="feedback-contact" className="mb-2 block text-sm font-medium">
                联系方式 <span className="text-xs font-normal text-muted">（可选，方便需要时回访）</span>
              </label>
              <input
                id="feedback-contact"
                type="text"
                value={contact}
                maxLength={FEEDBACK_CONTACT_MAX}
                onChange={(event) => setContact(event.target.value)}
                placeholder="邮箱或其他联系方式"
                className="w-full rounded-xl border border-border-light bg-bg px-3.5 py-2.5 text-sm text-fg outline-none transition-colors focus:border-accent/60 focus:ring-2 focus:ring-accent/15"
              />
            </div>

            <button
              type="submit"
              disabled={busy || !content.trim()}
              className="flex h-10 w-full items-center justify-center gap-2 rounded-xl bg-accent text-sm font-medium text-white transition-colors hover:bg-accent-hover disabled:opacity-60"
            >
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
              {busy ? "提交中…" : "提交反馈"}
            </button>
          </form>
        )}
      </main>
    </div>
  );
}
