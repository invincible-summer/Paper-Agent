"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, BookOpen, ChevronLeft, ChevronRight, PanelLeftClose, PanelRightClose,
  Languages, MessageSquare, Highlighter, Plus, Minus, Send, Square, Bookmark, Check,
  X, Maximize2, List, FileText, Pencil, Trash2, Scan } from "lucide-react";
import { useRouter } from "next/navigation";
import { useAuthStore } from "@/stores/auth";
import { PdfReader } from "./PdfReader";
import { ReaderMarkdown } from "./ReaderMarkdown";
import { documentPath, readerCall, readerAction, readerHref, type Anchor, type Position,
  type ReaderDocument, type ReaderNote, type ReaderThread, type Selection } from "@/lib/reader-api";
import "./reader.css";

const categories: Record<string, string> = { highlight: "划线", contribution: "贡献", evidence: "证据", question: "疑问", method: "可复用方法" };

export function ReadingWorkbench({ sessionId, attachmentId }: { sessionId: string; attachmentId: string }) {
  const router = useRouter();
  const checked = useAuthStore(s => s.checked);
  const user = useAuthStore(s => s.user);
  const [data, setData] = useState<ReaderDocument | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [leftOpen, setLeftOpen] = useState(true);
  const [rightOpen, setRightOpen] = useState(true);
  const [navTab, setNavTab] = useState<"outline" | "marks">("outline");
  const [tab, setTab] = useState<"assist" | "notes">("assist");
  const [page, setPage] = useState(1);
  const [zoom, setZoom] = useState(1);
  const [view, setView] = useState<Position["view"]>("continuous");
  const [goal, setGoal] = useState("理解方法");
  const [glossary, setGlossary] = useState("");
  const [settings, setSettings] = useState(false);
  const [jump, setJump] = useState({ page: 1, serial: 0 });
  const [regionMode, setRegionMode] = useState(false);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [selectedAnchor, setSelectedAnchor] = useState<Anchor | null>(null);
  const [autoTranslate, setAutoTranslate] = useState(false);
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [answerKind, setAnswerKind] = useState<"translate" | "ask">("ask");
  const [answerAnchor, setAnswerAnchor] = useState<Anchor | null>(null);
  const [thinking, setThinking] = useState("");
  const [thread, setThread] = useState<ReaderThread | null>(null);
  const [busy, setBusy] = useState(false);
  const [complete, setComplete] = useState(false);
  const [status, setStatus] = useState("");
  const [noteText, setNoteText] = useState("");
  const [category, setCategory] = useState("evidence");
  const [editingNote, setEditingNote] = useState<ReaderNote | null>(null);
  const [noteSaving, setNoteSaving] = useState(false);
  const [publishing, setPublishing] = useState<string | null>(null);
  const [savedStatus, setSavedStatus] = useState("已保存");
  const version = useRef(0);
  const lastPosition = useRef("");
  const positionChain = useRef<Promise<void>>(Promise.resolve());
  const controller = useRef<AbortController | null>(null);
  const currentAction = useRef("");
  const base = documentPath(sessionId, attachmentId);
  const questionRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    void useAuthStore.getState().hydrate();
    if (window.innerWidth <= 1200) setLeftOpen(false);
    if (window.innerWidth <= 850) setRightOpen(false);
  }, []);
  const load = useCallback(async () => {
    try {
      const doc = await readerCall<ReaderDocument>(base);
      setData(doc); setError("");
      const pos = doc.position;
      version.current = pos?.version || 0;
      if (pos && pos.fingerprint === doc.fingerprint) {
        setPage(pos.page); setZoom(pos.zoom); setView(pos.view); setGoal(pos.goal); setGlossary(pos.glossary);
        setJump({ page: pos.page, serial: Date.now() });
        lastPosition.current = JSON.stringify({ page: pos.page, zoom: pos.zoom, view: pos.view, goal: pos.goal,
          glossary: pos.glossary, fingerprint: doc.fingerprint });
      }
      const hash = new URLSearchParams(window.location.hash.slice(1));
      const id = hash.get("anchor");
      const source = doc.anchors.find(a => a.id === id && a.fingerprint === doc.fingerprint);
      if (source) {
        setSelectedAnchor(source); setSelection(source); setPage(source.page); setJump({ page: source.page, serial: Date.now() });
      } else if (hash.get("page")) {
        const p = Math.max(1, Math.min(doc.page_count, Number(hash.get("page")) || 1));
        setPage(p); setJump({ page: p, serial: Date.now() });
      }
    } catch (e) { setError(e instanceof Error ? e.message : "无法打开论文"); }
  }, [base]);
  useEffect(() => { if (checked) void load(); }, [checked, load]);
  useEffect(() => () => controller.current?.abort(), []);

  const goPage = useCallback((p: number) => {
    if (!data) return;
    const next = Math.max(1, Math.min(data.page_count, p));
    setPage(next); setJump({ page: next, serial: Date.now() });
    window.history.replaceState(null, "", `#page=${next}`);
  }, [data]);
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if ((event.target as HTMLElement)?.closest("input,textarea,select,[contenteditable]")) return;
      if (event.key === "ArrowRight") { event.preventDefault(); goPage(page + (view === "spread" ? 2 : 1)); }
      if (event.key === "ArrowLeft") { event.preventDefault(); goPage(page - (view === "spread" ? 2 : 1)); }
      if (event.key === "Escape") { setSelection(null); setSelectedAnchor(null); setRegionMode(false); setSettings(false); window.getSelection()?.removeAllRanges(); }
    };
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  }, [goPage, page, view]);

  useEffect(() => {
    if (!data) return;
    const payload = { page, zoom, view, goal, glossary, fingerprint: data.fingerprint };
    const encoded = JSON.stringify(payload);
    if (encoded === lastPosition.current) return;
    setSavedStatus("正在保存…");
    const timer = setTimeout(() => {
      positionChain.current = positionChain.current.then(async () => {
        try {
          const value = await readerCall<Position>(base + "/position", "PUT", { ...payload, expected_version: version.current });
          version.current = value.version; lastPosition.current = encoded; setSavedStatus("已保存");
        } catch (e) { setSavedStatus("未保存"); setError(e instanceof Error ? e.message : "保存失败"); }
      });
    }, 700);
    return () => clearTimeout(timer);
  }, [base, data?.fingerprint, page, zoom, view, goal, glossary]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (savedStatus !== "已保存" || noteText || busy) { event.preventDefault(); event.returnValue = ""; }
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [savedStatus, noteText, busy]);

  const select = useCallback((value: Selection) => {
    controller.current?.abort(); currentAction.current = "";
    setBusy(false); setSelection(value); setSelectedAnchor(null); setThread(null); setEditingNote(null);
    setAnswer(""); setThinking(""); setComplete(false);
    setTab("assist"); setRegionMode(false);
  }, []);
  const activateAnchor = (anchor: Anchor) => {
    setSelection(anchor); setSelectedAnchor(anchor); goPage(anchor.page); setRightOpen(true);
    window.history.replaceState(null, "", `#anchor=${anchor.id}`);
    const found = data?.threads.find(t => t.anchor_id === anchor.id);
    setThread(found || null); setAnswer(""); setComplete(false); setTab("assist");
  };
  const ensureAnchor = async (signal?: AbortSignal): Promise<Anchor> => {
    if (selectedAnchor) return selectedAnchor;
    if (!data) throw new Error("论文尚未加载");
    const source = selection || { page, quote: "", rects: [] };
    const a = await readerCall<Anchor>(base + "/anchors", "POST", { ...source, fingerprint: data.fingerprint }, signal);
    setSelectedAnchor(a);
    setData(d => d ? { ...d, anchors: [...d.anchors.filter(x => x.id !== a.id), a] } : d);
    return a;
  };
  const runAction = async (action: "translate" | "ask", customQuestion?: string) => {
    controller.current?.abort();
    const control = new AbortController(); controller.current = control; currentAction.current = action;
    setAnswerKind(action); setBusy(true); setComplete(false); setError(""); setAnswer(""); setThinking(""); setStatus("正在准备原文…"); setTab("assist"); setRightOpen(true);
    try {
      const a = await ensureAnchor(control.signal);
      if (control.signal.aborted) return;
      setAnswerAnchor(a);
      const q = customQuestion || question;
      let done = false;
      for await (const event of readerAction(base, { action, anchor_id: a.id, question: q,
        thread_id: thread?.anchor_id === a.id ? thread.id : null,
        request_id: crypto.randomUUID().replaceAll("-", "") }, control.signal)) {
        if (event.type === "answer") setAnswer(s => s + String(event.content || ""));
        if (event.type === "thinking") setThinking(s => s + String(event.content || ""));
        if (event.type === "step") setStatus(String(event.step || "正在助读…"));
        if (event.type === "tool_progress") setStatus(String(event.message || "正在查找依据…"));
        if (event.type === "error") throw new Error(String(event.message));
        if (event.type === "done") {
          done = true; setAnswer(String(event.answer)); setComplete(true); setStatus("已完成");
          if (event.thread) {
            const t = event.thread as ReaderThread; setThread(t);
            setData(d => d ? { ...d, threads: [...d.threads.filter(x => x.id !== t.id), t] } : d);
            setQuestion("");
          }
        }
      }
      if (!done && !control.signal.aborted) throw new Error("连接已结束，回答尚未完成，请重试");
    } catch (e) {
      if (!control.signal.aborted) setError(e instanceof Error ? e.message : "助读失败");
      else if (controller.current === control) setStatus("已停止，未完成回答未保存");
    } finally { if (controller.current === control) { setBusy(false); currentAction.current = ""; } }
  };
  const autoAction = useRef(runAction); autoAction.current = runAction;
  useEffect(() => {
    if (!autoTranslate || !selection?.quote || currentAction.current === "ask") return;
    const timer = setTimeout(() => { void autoAction.current("translate"); }, 450);
    return () => clearTimeout(timer);
  }, [selection, autoTranslate]);

  const saveNote = async (withAnswer = false, highlightOnly = false) => {
    setNoteSaving(true); setError("");
    try {
      const a = withAnswer && answerAnchor ? answerAnchor : await ensureAnchor();
      const body = { anchor_id: editingNote?.anchor_id || a.id, category: highlightOnly ? "highlight" : category,
        note: noteText, interpretation: withAnswer ? answer : editingNote?.interpretation || "",
        expected_version: editingNote?.version || 0 };
      const value = await readerCall<ReaderNote>(base + "/notes" + (editingNote ? `/${editingNote.id}` : ""), editingNote ? "PUT" : "POST", body);
      setData(d => d ? { ...d, notes: [...d.notes.filter(n => n.id !== value.id), value] } : d);
      setNoteText(""); setEditingNote(null); setNotice(highlightOnly ? "已保存划线" : "发现已记下，可带回主对话");
      if (!highlightOnly) { setTab("notes"); setRightOpen(true); }
      if (highlightOnly) { setSelection(null); window.getSelection()?.removeAllRanges(); }
    } catch (e) { setError(e instanceof Error ? e.message : "保存失败，草稿仍保留"); }
    finally { setNoteSaving(false); }
  };
  const publish = async (note: ReaderNote) => {
    setPublishing(note.id);
    try {
      await readerCall(base + `/notes/${note.id}/publish`, "POST");
      setNotice("已带回主对话，可以继续比较、写作或汇报");
      const channel = new BroadcastChannel("paper-reader"); channel.postMessage({ history: data?.history_filename }); channel.close();
    } catch (e) { setError(e instanceof Error ? e.message : "发送失败"); }
    finally { setPublishing(null); }
  };
  const removeNote = async (note: ReaderNote) => {
    try {
      await readerCall(base + `/notes/${note.id}?version=${note.version}`, "DELETE");
      setData(d => d ? { ...d, notes: d.notes.filter(n => n.id !== note.id) } : d);
    } catch (e) { setError(e instanceof Error ? e.message : "删除失败"); }
  };
  const back = () => {
    if (window.opener && !window.opener.closed) { window.opener.focus(); return; }
    router.push(`/chat?history=${encodeURIComponent(data?.history_filename || "")}`);
  };
  if (!data) return <div className="reading-workbench reader-loading"><BookOpen size={32} /><h1>阅研 · 阅读工作台</h1>
    {error ? <><p role="alert">{error}</p><button onClick={() => void load()}>重新加载</button><a href="/chat">返回对话 / 登录</a></> : <p>正在打开论文…</p>}</div>;
  const currentAnchors = data.anchors.filter(a => a.fingerprint === data.fingerprint);
  const marked = currentAnchors.filter(a => data.notes.some(n => n.anchor_id === a.id) || data.threads.some(t => t.anchor_id === a.id) || a.id === selectedAnchor?.id);
  const latestEditingNote = editingNote ? data.notes.find(n => n.id === editingNote.id) : undefined;
  const getAnchor = (id: string) => data.anchors.find(a => a.id === id);
  return <div className={`reading-workbench ${leftOpen ? "has-left" : ""} ${rightOpen ? "has-right" : ""}`}>
    <header className="reader-topbar">
      <button className="reader-back" onClick={back} title="返回所属对话"><ArrowLeft size={16} /><span>对话</span></button>
      <div className="reader-brand"><BookOpen size={21} /><strong>阅研</strong><span>READING STUDIO</span></div>
      <div className="reader-document-title"><small>{data.title}</small>
        <select aria-label="切换当前会话论文" value={attachmentId} onChange={e => {
          if (noteText || busy || savedStatus !== "已保存") { setError("请先保存笔记并等待任务完成，再切换论文"); return; }
          router.push(readerHref(sessionId, e.target.value));
        }}>{data.documents.map(d => <option key={d.id} value={d.id}>{d.filename}</option>)}</select>
      </div>
      <span className={`reader-save-status ${savedStatus === "未保存" ? "unsaved" : ""}`}><Check size={12} />{savedStatus}</span>
      <button className="reader-icon" title="专注阅读" onClick={() => { const close = leftOpen || rightOpen; setLeftOpen(!close); setRightOpen(!close); }}><Maximize2 size={17} /></button>
    </header>
    <div className="reader-toolbar">
      <div className="reader-toolbar-group"><button className="reader-icon" title="切换目录" onClick={() => setLeftOpen(!leftOpen)}><List size={17} /></button>
        <span className="reader-toolbar-label">阅读目标</span><select aria-label="阅读目标" value={goal} onChange={e => setGoal(e.target.value)}>
          {["理解方法", "检查证据", "准备复现", "准备汇报"].map(g => <option key={g}>{g}</option>)}</select>
      </div>
      <div className="reader-toolbar-group"><select aria-label="阅读视图" value={view} onChange={e => setView(e.target.value as Position["view"])}>
        <option value="continuous">连续阅读</option><option value="single">单页翻阅</option><option value="spread">双页展开</option></select>
        <button className="reader-icon" title="缩小" onClick={() => setZoom(z => Math.max(.5, +(z - .1).toFixed(1)))}><Minus size={15} /></button>
        <button className="reader-zoom" title="恢复适合宽度" onClick={() => setZoom(1)}>{Math.round(zoom * 100)}%</button>
        <button className="reader-icon" title="放大" onClick={() => setZoom(z => Math.min(3, +(z + .1).toFixed(1)))}><Plus size={15} /></button>
      </div>
      <div className="reader-toolbar-group"><button className="reader-region-toggle" aria-pressed={regionMode} onClick={() => setRegionMode(!regionMode)}><Scan size={15} />{regionMode ? "取消框选" : "框选图片"}</button><label className="reader-auto"><input type="checkbox" checked={autoTranslate} onChange={e => setAutoTranslate(e.target.checked)} />划选即译</label>
        <button className="reader-icon" title="展开助读" onClick={() => setRightOpen(!rightOpen)}><MessageSquare size={17} /></button></div>
    </div>
    {(error || notice) && <div className={error ? "reader-banner error" : "reader-banner"} role={error ? "alert" : "status"}>
      <span>{error || notice}</span>{error && <button onClick={() => { void load(); }}>刷新记录</button>}<button aria-label="关闭提示" onClick={() => { setError(""); setNotice(""); }}><X size={14} /></button></div>}
    <div className="reader-body">
      {leftOpen && <aside className="reader-nav"><div className="reader-panel-head"><div className="reader-tabs">
        <button className={navTab === "outline" ? "active" : ""} onClick={() => setNavTab("outline")}>目录</button>
        <button className={navTab === "marks" ? "active" : ""} onClick={() => setNavTab("marks")}>标记 <small>{marked.length}</small></button></div>
        <button className="reader-icon" title="收起目录" onClick={() => setLeftOpen(false)}><PanelLeftClose size={16} /></button></div>
        <div className="reader-nav-content">{navTab === "outline" ? <>
          <div className="reader-nav-caption">PAPER OUTLINE</div>
          {data.outline.length ? data.outline.map((item, i) => <button key={i} className={`reader-outline-item ${page === item.page ? "active" : ""}`} style={{ paddingLeft: 12 + Math.min(item.level - 1, 3) * 12 }} onClick={() => goPage(item.page)}>
            <span>{item.title}</span><small>{item.page}</small></button>) : <div className="reader-empty"><FileText size={23} /><p>这篇 PDF 没有内置目录</p><small>按页翻阅或在原文中选句，助读随时可用。</small></div>}
          <div className="reader-nav-caption">PAGE NAVIGATION</div><div className="reader-page-grid">{Array.from({ length: Math.min(data.page_count, 200) }, (_, i) => <button key={i} className={page === i + 1 ? "active" : ""} onClick={() => goPage(i + 1)}>{i + 1}</button>)}</div>
        </> : marked.length ? marked.map(a => <button key={a.id} className="reader-mark-item" onClick={() => activateAnchor(a)}><small>第 {a.page} 页 · {a.precision === "text" ? "原文标记" : a.precision === "region" ? "图文区域" : "页面标记"}</small><p>{a.quote || (a.precision === "region" ? "框选图文区域" : "整页阅读讨论")}</p></button>)
          : <div className="reader-empty"><Bookmark size={24} /><p>留下你的阅读线索</p><small>选中原文记下发现，或围绕一句话展开讨论。</small></div>}
        </div><div className="reader-nav-bottom">{user?.display_name || "我的"}阅读空间<br /><span>原文 · 理解 · 判断</span></div>
      </aside>}
      <main className="reader-main">
        <PdfReader url={base + "/content"} page={page} zoom={zoom} view={view} jump={jump} anchors={marked} activeAnchor={selectedAnchor?.id}
          selection={selection} regionMode={regionMode} savedAnchorIds={data.notes.map(n => n.anchor_id)}
          onPage={setPage} onSelection={select} onAnchor={activateAnchor} />
        {selection && <div className="reader-selection-bar" role="toolbar" aria-label="选文操作" onMouseDown={e => e.preventDefault()}>
          <small>第 {selection.page} 页 · {selection.quote ? `已选 ${selection.quote.length} 字` : selection.rects.length ? "已选图文区域" : "当前页"}</small>{selection.quote && <button onClick={() => void runAction("translate")}><Languages size={15} />翻译</button>}
          <button onClick={() => void runAction("ask", selection.quote ? "请解释选中这段话的含义、必要背景和适用条件。" : "请结合这一页解释框选的图表或公式，说明依据；若无法读取图像内容请明确说明。" )}><BookOpen size={15} />解释</button>
          <button onClick={() => { setRightOpen(true); setTab("assist"); questionRef.current?.focus(); }}><MessageSquare size={15} />追问</button>
          <button disabled={noteSaving} onClick={() => void saveNote(false, true)}><Highlighter size={15} />{selection.quote ? "划线" : "标记区域"}</button>
          <button onClick={() => { setTab("notes"); setRightOpen(true); }}><Bookmark size={15} />记下</button>
          <button aria-label="取消选区" onClick={() => { setSelection(null); setSelectedAnchor(null); window.getSelection()?.removeAllRanges(); }}><X size={13} /></button>
        </div>}
        <footer className="reader-pagination"><button className="reader-icon" title="上一页" disabled={page <= 1} onClick={() => goPage(page - 1)}><ChevronLeft size={17} /></button>
          <label><input aria-label="跳转页码" type="number" min={1} max={data.page_count} value={page} onChange={e => goPage(Number(e.target.value))} /> / {data.page_count}</label>
          <button className="reader-icon" title="下一页" disabled={page >= data.page_count} onClick={() => goPage(page + 1)}><ChevronRight size={17} /></button>
          <span>原版 PDF · {Math.round(page / data.page_count * 100)}% 已浏览</span></footer>
      </main>
      {rightOpen && <aside className="reader-assist"><div className="reader-panel-head"><div className="reader-tabs">
        <button className={tab === "assist" ? "active" : ""} onClick={() => setTab("assist")}>助读</button>
        <button className={tab === "notes" ? "active" : ""} onClick={() => setTab("notes")}>我的发现 <small>{data.notes.length}</small></button></div>
        <button className="reader-icon" title="收起助读" onClick={() => setRightOpen(false)}><PanelRightClose size={16} /></button></div>
        <div className="reader-assist-scroll">
          {tab === "assist" ? <>
            {!answer && !thread && !busy && <div className="reader-welcome"><div className="reader-welcome-icon"><BookOpen size={25} /></div><h2>和论文一起思考</h2><p>读懂一句话，也追问它为什么成立。解释与原文始终相连。</p>
              <div className="reader-question-presets">{["这一页的核心意思是什么？", "总结这篇论文的问题、方法、结论和局限。", "这段论述依赖哪些假设？", "请解释这里的公式或符号。"].map(q => <button key={q} onClick={() => void runAction("ask", q)}>{q}<Plus size={13} /></button>)}</div></div>}
            {thread && <div className="reader-thread"><div className="reader-thread-heading"><span>页边讨论 · {thread.messages.length / 2} 轮</span><button onClick={() => { setThread(null); setAnswer(""); setComplete(false); }}>新讨论</button></div>
              {thread.messages.map((m, i) => <div className={`reader-thread-message ${m.role}`} key={i}><small>{m.role === "user" ? "我" : "阅研 · 助读"}</small><ReaderMarkdown onPage={goPage}>{m.content}</ReaderMarkdown>
                {m.role === "assistant" && <button className="reader-source" onClick={() => { const a = getAnchor(thread.anchor_id); if (a) { setAnswer(m.content); setAnswerAnchor(a); setComplete(true); setTab("notes"); } }}>记为发现</button>}</div>)}</div>}
            {(busy || (answer && (!thread || answerKind === "translate"))) && <div className="reader-answer"><div className="reader-answer-heading"><Languages size={15} /><span>{busy ? status : "助读结果"}</span>{busy && <span className="reader-spinner" />}</div>
              {thinking && <details className="reader-thinking"><summary>思考过程</summary><ReaderMarkdown>{thinking}</ReaderMarkdown></details>}
              {answer && <ReaderMarkdown onPage={goPage}>{answer}</ReaderMarkdown>}
              {answerAnchor && <button className="reader-source" onClick={() => activateAnchor(answerAnchor)}>↗ 第 {answerAnchor.page} 页 · {answerAnchor.precision === "text" ? "提问原文" : answerAnchor.precision === "region" ? "框选区域" : "提问页面"}</button>}
              {complete && <button className="reader-save-answer" onClick={() => { setTab("notes"); }}>记为发现 <Bookmark size={14} /></button>}
            </div>}
            <button className="reader-glossary-toggle" onClick={() => setSettings(!settings)}><Languages size={14} />本篇术语表 {settings ? "−" : "+"}</button>
            {settings && <div className="reader-glossary"><label htmlFor="reader-glossary">专业译法（随阅读自动保存）</label><textarea id="reader-glossary" maxLength={4000} value={glossary} onChange={e => setGlossary(e.target.value)} placeholder="例如：ablation = 消融实验\ngeneralization = 泛化能力" /><small>只影响这篇论文在当前会话中的后续翻译。</small></div>}
          </> : <>
            <div className="reader-note-editor"><h3>{editingNote ? "编辑我的笔记" : "留下一个发现"}</h3>
              <select aria-label="发现分类" value={category} onChange={e => setCategory(e.target.value)}>{Object.entries(categories).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select>
              <textarea aria-label="我的阅读笔记" value={noteText} maxLength={10000} onChange={e => setNoteText(e.target.value)} placeholder="它对我的研究意味着什么？有哪些条件值得继续核对？" />
              {complete && answer && !editingNote && <div className="reader-note-answer-preview"><small>可附上助读解释 · 第 {answerAnchor?.page} 页</small><p>{answer.slice(0, 180)}{answer.length > 180 ? "…" : ""}</p></div>}
              {editingNote && latestEditingNote && latestEditingNote.version !== editingNote.version && <div className="reader-note-conflict">
                <small>该笔记已更新。下方发现卡显示最新内容，当前草稿仍保留。</small>
                <button onClick={() => setEditingNote(latestEditingNote)}>已核对，基于新版继续保存草稿</button>
              </div>}
              <div className="reader-note-editor-actions"><button className="reader-primary" disabled={noteSaving || (!noteText.trim() && !selection && !editingNote)} onClick={() => void saveNote()}>{noteSaving ? "保存中…" : editingNote ? "保存修改" : "保存笔记"}</button>
                {complete && answer && !editingNote && <button disabled={noteSaving} onClick={() => void saveNote(true)}>附解释保存</button>}
                {editingNote && <button onClick={() => { setEditingNote(null); setNoteText(""); }}>取消编辑</button>}</div>
            </div>
            <div className="reader-findings-heading">本篇发现 <span>{data.notes.length}</span></div>
            {!data.notes.length && <div className="reader-empty"><Bookmark size={24} /><p>读过的东西，可以留下来</p><small>保存原文、解释和自己的判断，带回对话继续研究。</small></div>}
            {[...data.notes].reverse().map(n => { const a = getAnchor(n.anchor_id); const stale = a?.fingerprint !== data.fingerprint; return <article className="reader-finding" key={n.id}>
              <div className="reader-finding-meta"><span className={`reader-category ${n.category}`}>{categories[n.category]}</span><button disabled={stale} onClick={() => a && activateAnchor(a)}>第 {a?.page} 页 ↗</button></div>
              {stale && <small>原文件版本已改变，保留笔记供核对</small>}
              {a?.quote && <blockquote>{a.quote}</blockquote>}
              {n.interpretation && <details><summary>助读解释</summary><ReaderMarkdown onPage={goPage}>{n.interpretation}</ReaderMarkdown></details>}
              {n.note && <p className="reader-my-note"><small>我的判断</small>{n.note}</p>}
              <div className="reader-finding-actions"><button disabled={!!publishing || stale} onClick={() => void publish(n)}><Send size={12} />{publishing === n.id ? "发送中…" : "带回对话"}</button>
                <button title="编辑笔记" disabled={stale} onClick={() => { setEditingNote(n); setNoteText(n.note); setCategory(n.category); if (a) { setSelection(a); setSelectedAnchor(a); } }}><Pencil size={13} /></button>
                <button title="删除笔记" onClick={() => void removeNote(n)}><Trash2 size={13} /></button></div>
            </article>; })}
          </>}
        </div>
        {tab === "assist" && <div className="reader-composer"><div className="reader-composer-scope"><span />当前论文{selectedAnchor ? ` · 第 ${selectedAnchor.page} 页` : ` · 第 ${page} 页`}</div>
          <textarea ref={questionRef} aria-label="阅读问题" maxLength={3000} value={question} onChange={e => setQuestion(e.target.value)} placeholder="对这一段，有什么想问的？" onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey && question.trim() && !busy) { e.preventDefault(); void runAction("ask"); } }} />
          <div><small>Enter 提问 · Shift Enter 换行</small>{busy ? <button className="reader-primary" onClick={() => controller.current?.abort()}><Square size={13} />停止</button> : <button className="reader-primary" disabled={!question.trim()} onClick={() => void runAction("ask")}><Send size={14} />提问</button>}</div>
        </div>}
      </aside>}
    </div>
  </div>;
}
