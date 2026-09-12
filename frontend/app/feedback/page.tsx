"use client";

import { useState } from "react";
import { MessageSquarePlus, Send, Loader2, ArrowLeft, CheckCircle2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { AppFrame } from "@/components/AppFrame";
import { EmptyState, PageHeader, SectionPanel, StatusNotice } from "@/components/WorkbenchUI";
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
    <AppFrame>
      <div className="library-canvas !max-w-[860px] flex-1 overflow-y-auto">
        <PageHeader eyebrow="Feedback" title="意见反馈" description="问题报告与功能建议会直接送达管理员，帮助我们持续改进" icon={<MessageSquarePlus className="h-5 w-5" />} actions={<button type="button" onClick={() => router.push("/chat")} className="btn-secondary"><ArrowLeft className="h-3.5 w-3.5" />返回对话</button>} />

        {done ? (
          <section className="workbench-panel"><EmptyState icon={<CheckCircle2 className="h-9 w-9 text-success" />} title="感谢你的反馈！" description={`我们已收到你的${category === "功能建议" ? "建议" : "反馈"}，管理员会在后台查看处理。`} />
            <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
              <button
                type="button"
                onClick={() => { setDone(false); setContent(""); setContact(""); setCategory("问题报告"); }}
                className="flex h-9 items-center gap-1.5 rounded-[7px] border border-border-light px-4 text-[13px] text-fg-secondary transition-colors hover:bg-surface-hover hover:text-fg"
              >
                再写一条
              </button>
              <button
                type="button"
                onClick={() => router.push("/chat")}
                className="flex h-9 items-center rounded-[7px] bg-accent px-5 text-[13px] font-medium text-white transition-colors hover:bg-accent-hover"
              >
                返回对话
              </button>
            </div>
          </section>
        ) : (
          <SectionPanel>
          <form onSubmit={onSubmit} className="space-y-5 p-6">
            {error && (
              <StatusNotice tone="warning">{error}</StatusNotice>
            )}

            <div>
              <span className="mb-2 block text-[13px] font-medium">反馈类型</span>
              <div className="flex flex-wrap gap-2">
                {FEEDBACK_CATEGORIES.map((item) => (
                  <button
                    key={item} type="button" onClick={() => setCategory(item)}
                    aria-pressed={category === item}
                    className={`h-9 rounded-[7px] border px-4 text-[13px] transition-colors ${
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
              <label htmlFor="feedback-content" className="mb-2 block text-[13px] font-medium">
                反馈内容 <span className="text-error">*</span>
              </label>
              <textarea
                id="feedback-content"
                value={content}
                onChange={(event) => setContent(event.target.value.slice(0, FEEDBACK_CONTENT_MAX))}
                placeholder="请描述遇到的问题或想要的功能，例如操作步骤、期望效果等"
                rows={7}
                required
                className="w-full resize-y rounded-[7px] border border-border-light bg-bg p-3.5 text-[13px] leading-6 text-fg outline-none transition-colors focus:border-accent/60 focus:ring-2 focus:ring-accent/15"
              />
              <p className={`mt-1.5 text-right text-[12px] ${contentLeft < 200 ? "text-warning" : "text-muted"}`}>
                还可输入 {contentLeft} 字
              </p>
            </div>

            <div>
              <label htmlFor="feedback-contact" className="mb-2 block text-[13px] font-medium">
                联系方式 <span className="text-[12px] font-normal text-muted">（可选，方便需要时回访）</span>
              </label>
              <input
                id="feedback-contact"
                type="text"
                value={contact}
                maxLength={FEEDBACK_CONTACT_MAX}
                onChange={(event) => setContact(event.target.value)}
                placeholder="邮箱或其他联系方式"
                className="w-full rounded-[7px] border border-border-light bg-bg px-3.5 py-2.5 text-[13px] text-fg outline-none transition-colors focus:border-accent/60 focus:ring-2 focus:ring-accent/15"
              />
            </div>

            <button
              type="submit"
              disabled={busy || !content.trim()}
              className="flex h-10 w-full items-center justify-center gap-2 rounded-[7px] bg-accent text-[13px] font-medium text-white transition-colors hover:bg-accent-hover disabled:opacity-60"
            >
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
              {busy ? "提交中…" : "提交反馈"}
            </button>
          </form>
          </SectionPanel>
        )}
      </div>
    </AppFrame>
  );
}
