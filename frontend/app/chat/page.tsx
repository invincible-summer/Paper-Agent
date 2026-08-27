"use client";
import { useEffect, useRef, useCallback } from "react";
import { Search, Map, FileText, Route, ArrowRight, Sparkles } from "lucide-react";
import { useChatStore } from "@/stores/chat";
import { useUIStore } from "@/stores/ui";
import { chatStream, listChatHistory, type ChatAttachment } from "@/lib/chat-api";
import { ChatMessage, StreamingMessage } from "@/components/chat/ChatMessage";
import { ChatInput } from "@/components/chat/ChatInput";
import { AppShell } from "@/components/AppShell";
import { useHistoryLoader } from "@/lib/history-loader";

const SUGGESTIONS = [
  { icon: Search, text: "检索图神经网络在推荐系统中的应用", desc: "多源检索 + 语义重排" },
  { icon: Map, text: "先检索启蒙理性相关论文，再生成研究地图", desc: "主题簇 + 谱系图" },
  { icon: Route, text: "检索扩散模型论文并推荐阅读路径", desc: "奠基 → 桥梁 → 前沿" },
  { icon: FileText, text: "检索后帮我写一份文献综述", desc: "按主题簇组织" },
];

export default function ChatPage() {
  const store = useChatStore();
  const { uiLang } = store;
  const scrollRef = useRef<HTMLDivElement>(null);
  // AbortController for the in-flight turn (stop button).
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    listChatHistory().then((data) => {
      store.setHistoryList(data.records);
    }).catch(() => {});
  }, []); // eslint-disable-line

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    // Only auto-scroll when the user is already near the bottom — otherwise
    // fighting the user's scroll position also forces a layout per flush.
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 240;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [store.messages, store.currentThinking, store.currentAnswer, store.toolProgress]);

  const handleSend = useCallback(async (message: string, attachments?: ChatAttachment[], opts?: { regenerate?: boolean }) => {
    // Actions read via getState() keep this callback's identity stable across
    // streaming re-renders, so memoized ChatMessage props never change.
    const store = useChatStore.getState();
    const regenerate = !!opts?.regenerate;
    // On regenerate the prior user message is already in the list; only drop
    // the trailing assistant message.
    if (!regenerate) {
      store.addMessage({ role: "user", content: message, attachments });
    }
    store.startResponding();
    const ac = new AbortController();
    abortRef.current = ac;

    let thinkingAccum = "";
    let answerAccum = "";
    const toolCallsAccum: { name: string; result?: unknown }[] = [];

    // Buffer high-frequency deltas and flush ~16x/s. Per-token setState
    // re-renders the page and re-parses the streaming markdown — that was
    // the visible stutter. Terminal events flush immediately (ordering).
    const buf = { thinking: "", answer: "" };
    let flushTimer: ReturnType<typeof setTimeout> | null = null;
    const flush = () => {
      if (flushTimer != null) { clearTimeout(flushTimer); flushTimer = null; }
      const s = useChatStore.getState();
      if (buf.thinking) { s.appendThinking(buf.thinking); buf.thinking = ""; }
      if (buf.answer) { s.appendAnswer(buf.answer); buf.answer = ""; }
    };
    const scheduleFlush = () => {
      if (flushTimer != null) return;
      flushTimer = setTimeout(() => { flushTimer = null; flush(); }, 60);
    };

    try {
      const stream = chatStream({
        message,
        history_filename: store.currentChatFilename,
        topic: store.topic,
        language: uiLang === "zh" ? "zh" : "en",
        attachments,
        regenerate,
      }, ac.signal);

      for await (const event of stream) {
        switch (event.type) {
          case "thinking":
            thinkingAccum += event.content as string;
            buf.thinking += event.content as string;
            scheduleFlush();
            break;
          case "answer":
            answerAccum += event.content as string;
            buf.answer += event.content as string;
            scheduleFlush();
            break;
          case "tool_start":
            flush();
            if (event.name === "use_skill") break;
            store.addToolCall(event.name as string);
            toolCallsAccum.push({ name: event.name as string });
            break;
          case "tool_progress":
            flush();
            store.addToolProgress(event.message as string);
            break;
          case "tool_result": {
            flush();
            const result = event.result as {
              tool?: string;
              attachments?: Array<{
                id?: string; status?: string; multimodal_status?: string;
                element_count?: number; char_count?: number;
              }>;
            } | undefined;
            if (result?.tool === "use_skill") break;
            if (toolCallsAccum.length > 0) {
              toolCallsAccum[toolCallsAccum.length - 1].result = event.result;
            }
            store.updateToolCallResult(event.result);
            const updates = (result?.attachments || [])
              .filter((a): a is { id: string; status?: string; multimodal_status?: string; element_count?: number; char_count?: number } => Boolean(a.id))
              .map((a) => ({
                id: a.id,
                multimodal_status: a.status || a.multimodal_status,
                element_count: a.element_count,
                ...(typeof a.char_count === "number" ? { char_count: a.char_count } : {}),
              }));
            if (updates.length) store.patchAttachments(updates);
            break;
          }
          case "tool_warning":
            flush();
            store.addToolProgress(`⚠ ${event.warning}`);
            break;
          case "step":
            store.setCurrentStep(event.step as string);
            store.setHeartbeatElapsed(0);
            break;
          case "heartbeat":
            store.setHeartbeatElapsed(event.elapsed as number);
            break;
          case "history_saved":
            store.setCurrentChatFilename(event.filename as string);
            listChatHistory().then((data) => store.setHistoryList(data.records));
            break;
          case "done":
            flush();
            store.addMessage({
              role: "assistant",
              content: answerAccum || thinkingAccum,
              thinking: thinkingAccum,
              toolCalls: toolCallsAccum,
            });
            break;
          case "error":
            flush();
            store.addMessage({
              role: "assistant",
              content: `[错误] ${event.message}`,
            });
            break;
        }
      }
    } catch (e) {
      // User-initiated stop: commit whatever partial answer/thinking we
      // already received as an assistant message.
      flush();
      const aborted = ac.signal.aborted || (e instanceof DOMException && e.name === "AbortError");
      if (aborted) {
        if (answerAccum || thinkingAccum || toolCallsAccum.length > 0) {
          store.addMessage({
            role: "assistant",
            content: answerAccum || thinkingAccum || "(已中断)",
            thinking: thinkingAccum,
            toolCalls: toolCallsAccum,
          });
        }
      } else {
        store.addMessage({
          role: "assistant",
          content: `[连接错误] ${e instanceof Error ? e.message : String(e)}`,
        });
      }
    } finally {
      abortRef.current = null;
      store.stopResponding();
      store.clearStreaming();
    }
  }, [uiLang]);

  const handleStop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  // Regenerate the last assistant response: pop it, then re-run the
  // preceding user message with regenerate=true.
  const handleRegenerate = useCallback(() => {
    const msgs = useChatStore.getState().messages;
    let lastAssistantIdx = -1;
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === "assistant") { lastAssistantIdx = i; break; }
    }
    if (lastAssistantIdx < 1) return;
    const userMsg = msgs[lastAssistantIdx - 1];
    if (!userMsg || userMsg.role !== "user") return;
    const trimmed = msgs.slice(0, lastAssistantIdx);
    useChatStore.getState().setMessages(trimmed);
    void handleSend(userMsg.content, undefined, { regenerate: true });
  }, [handleSend]);

  const handleNewChat = () => {
    store.reset();
    useUIStore.getState().clearActiveFile();
  };
  const loadHistory = useHistoryLoader();
  const handleSelectHistory = useCallback((filename: string) => {
    useUIStore.getState().clearActiveFile();
    void loadHistory(filename);
  }, [loadHistory]);

  const isEmpty = store.messages.length === 0 && !store.isResponding;

  return (
    <AppShell onNewSession={handleNewChat} onSelectHistory={handleSelectHistory}>
      <div className="flex flex-1 flex-col overflow-hidden">
        <div ref={scrollRef} className="flex-1 overflow-y-auto">
          {isEmpty ? (
            <div className="flex min-h-full flex-col items-center justify-center px-6">
              <div className="mb-6 flex h-14 w-14 items-center justify-center rounded-[18px] bg-gradient-to-br from-accent to-accent-hover shadow-lg shadow-accent/25">
                <Sparkles className="h-7 w-7 text-white" />
              </div>
              <h1 className="mb-2 text-2xl font-bold tracking-tight text-fg">
                你好，我是 Paper Agent
              </h1>
              <p className="mb-8 max-w-md text-center text-[14px] leading-relaxed text-muted">
                告诉我你的研究主题——我来检索文献、深读论文、绘制研究地图与谱系图、
                规划阅读路径、撰写文献综述。
              </p>
              <div className="grid w-full max-w-lg grid-cols-2 gap-2.5">
                {SUGGESTIONS.map((s) => (
                  <button
                    key={s.text}
                    onClick={() => handleSend(s.text)}
                    className="group flex items-start gap-3 rounded-xl border border-border-light bg-surface px-4 py-3 text-left shadow-sm transition-all hover:-translate-y-px hover:border-accent/30 hover:shadow-md"
                  >
                    <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-accent-soft/60 transition-colors group-hover:bg-accent-soft">
                      <s.icon className="h-4 w-4 text-accent" />
                    </div>
                    <div className="min-w-0">
                      <p className="text-[13px] font-medium text-fg">{s.text}</p>
                      <p className="mt-0.5 text-[11px] text-muted/60">{s.desc}</p>
                    </div>
                    <ArrowRight className="ml-auto h-3.5 w-3.5 shrink-0 text-muted/30 transition-all group-hover:translate-x-0.5 group-hover:text-accent/50" />
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="mx-auto max-w-[760px] px-4 pb-4">
              {store.messages.map((msg, i) => (
                <ChatMessage key={i} msg={msg}
                  isLast={!store.isResponding && i === store.messages.length - 1 && msg.role === "assistant"}
                  disabled={store.isResponding}
                  onRegenerate={handleRegenerate}
                />
              ))}
              {store.isResponding && (
                <StreamingMessage
                  thinking={store.currentThinking}
                  answer={store.currentAnswer}
                  activeTool={store.activeTool}
                  toolProgress={store.toolProgress}
                  toolCalls={store.currentToolCalls}
                  currentStep={store.currentStep}
                  heartbeatElapsed={store.heartbeatElapsed}
                />
              )}
            </div>
          )}
        </div>

        <ChatInput onSend={handleSend} onStop={handleStop} disabled={store.isResponding} />
      </div>
    </AppShell>
  );
}
