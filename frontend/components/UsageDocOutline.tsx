"use client";

import { useCallback, useEffect, useRef, useState, type MouseEvent } from "react";
import { ListTree } from "lucide-react";

export type UsageDocOutlineItem = {
  level: number;
  text: string;
  line: number;
  id: string;
  isTitleHeading: boolean;
};

const HEADING_RE = /^(#{1,6})\s+(.+?)\s*#*$/;
const FENCE_RE = /^\s*(?:```|~~~)/;
const MAX_INDENT_DEPTH = 3;
// 预留 sticky 顶栏 + 页边距，避免锚点滚动后标题被遮挡；需不小于标题的
// scroll-mt-28(112px) 落点，跳转到的章节才能立即高亮。
const ACTIVE_OFFSET_PX = 120;

function stripInlineMarkdown(raw: string): string {
  return raw
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/<[^>]+>/g, "")
    .replace(/[*_~`]+/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

export function extractUsageDocOutline(content: string): UsageDocOutlineItem[] {
  const items: UsageDocOutlineItem[] = [];
  const lines = content.split("\n");
  let inFence = false;
  let sawContent = false;
  let titleHeadingSeen = false;
  lines.forEach((line, index) => {
    if (FENCE_RE.test(line)) {
      inFence = !inFence;
      return;
    }
    if (inFence || !line.trim()) return;
    const match = HEADING_RE.exec(line);
    if (!match) {
      sawContent = true;
      return;
    }
    const text = stripInlineMarkdown(match[2]);
    if (!text) {
      sawContent = true;
      return;
    }
    // 手册自带的行首一级标题与页面固定大标题重复：不进目录、不渲染。
    const isTitleHeading = !titleHeadingSeen && !sawContent && match[1].length === 1;
    if (isTitleHeading) titleHeadingSeen = true;
    items.push({
      level: match[1].length,
      text,
      line: index + 1,
      id: `usage-doc-sec-${items.length}`,
      isTitleHeading,
    });
    sawContent = true;
  });
  return items;
}

export function outlineEntries(items: UsageDocOutlineItem[]): UsageDocOutlineItem[] {
  return items.filter((item) => !item.isTitleHeading);
}

function useOutlineNavigation(entries: UsageDocOutlineItem[]) {
  const [activeId, setActiveId] = useState<string | null>(entries[0]?.id ?? null);
  // 手册较短时页面会先到底部、目标章节到不了视口顶部，自动高亮会停在
  // 上一节；点击后先锁定点击项，用户手动滚动再恢复自动跟踪。
  const pendingRef = useRef<string | null>(null);

  useEffect(() => {
    if (entries.length === 0) return;
    const update = () => {
      const pending = pendingRef.current;
      if (pending) {
        const element = document.getElementById(pending);
        if (!element) {
          pendingRef.current = null;
        } else if (element.getBoundingClientRect().top - ACTIVE_OFFSET_PX > 0) {
          setActiveId(pending);
          return;
        } else {
          pendingRef.current = null;
        }
      }
      let current = entries[0].id;
      for (const item of entries) {
        const element = document.getElementById(item.id);
        if (element && element.getBoundingClientRect().top - ACTIVE_OFFSET_PX <= 0) {
          current = item.id;
        }
      }
      // 已滚到页面底部时，正在阅读的就是最后一节。
      if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2) {
        current = entries[entries.length - 1].id;
      }
      setActiveId(current);
    };
    // 用户主动滚动时立即解除锁定并重算；页面已滚到底时 wheel 不产生
    // scroll 事件，不主动重算会残留点击项的高亮。
    const clearPending = () => {
      if (pendingRef.current !== null) {
        pendingRef.current = null;
        update();
      }
    };
    update();
    window.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    window.addEventListener("wheel", clearPending, { passive: true });
    window.addEventListener("touchmove", clearPending, { passive: true });
    window.addEventListener("keydown", clearPending);
    return () => {
      window.removeEventListener("scroll", update);
      window.removeEventListener("resize", update);
      window.removeEventListener("wheel", clearPending);
      window.removeEventListener("touchmove", clearPending);
      window.removeEventListener("keydown", clearPending);
    };
  }, [entries]);

  const scrollTo = useCallback((id: string) => {
    pendingRef.current = id;
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, []);

  return { activeId, scrollTo };
}

type OutlineNavigation = ReturnType<typeof useOutlineNavigation>;

function handleOutlineClick(
  event: MouseEvent<HTMLAnchorElement>,
  id: string,
  scrollTo: (id: string) => void
): void {
  event.preventDefault();
  scrollTo(id);
  if (window.innerWidth < 1024) {
    event.currentTarget.closest("details")?.removeAttribute("open");
  }
}

const INDENT_CLASSES = ["pl-2", "pl-3.5", "pl-5", "pl-6"];

export function UsageDocOutline({ items }: { items: UsageDocOutlineItem[] }) {
  const entries = outlineEntries(items);
  const { activeId, scrollTo } = useOutlineNavigation(entries);
  if (entries.length === 0) return null;
  const minLevel = Math.min(...entries.map((item) => item.level));

  return (
    <nav aria-label="本页目录" className="hidden w-56 shrink-0 lg:block">
      <div className="sticky top-24 max-h-[calc(100vh-7rem)] overflow-y-auto py-1 pr-2">
        <p className="mb-2 flex items-center gap-1.5 text-[12px] font-semibold tracking-wide text-muted">
          <ListTree className="h-3.5 w-3.5" />
          本页目录
        </p>
        <ul className="border-l border-border-light">
          {entries.map((item) => {
            const depth = Math.min(Math.max(item.level - minLevel, 0), MAX_INDENT_DEPTH);
            const active = item.id === activeId;
            return (
              <li key={item.id}>
                <a
                  href={`#${item.id}`}
                  onClick={(event) => handleOutlineClick(event, item.id, scrollTo)}
                  title={item.text}
                  className={[
                    "-ml-px block truncate border-l py-1 pr-2 text-[13px] leading-5 transition-colors",
                    INDENT_CLASSES[depth],
                    active
                      ? "border-accent font-medium text-accent"
                      : "border-transparent text-fg-secondary hover:text-fg",
                  ].join(" ")}
                >
                  {item.text}
                </a>
              </li>
            );
          })}
        </ul>
      </div>
    </nav>
  );
}

export function UsageDocMobileOutline({ items }: { items: UsageDocOutlineItem[] }) {
  const entries = outlineEntries(items);
  const { activeId, scrollTo }: OutlineNavigation = useOutlineNavigation(entries);
  if (entries.length === 0) return null;
  const minLevel = Math.min(...entries.map((item) => item.level));

  return (
    <details className="mb-4 rounded-[7px] border border-border-light bg-surface lg:hidden">
      <summary className="flex cursor-pointer select-none items-center gap-1.5 px-4 py-3 text-[13px] font-medium text-fg-secondary">
        <ListTree className="h-4 w-4" />
        本页目录
      </summary>
      <ul className="border-t border-border-light px-4 py-2">
        {entries.map((item) => {
          const depth = Math.min(Math.max(item.level - minLevel, 0), MAX_INDENT_DEPTH);
          const active = item.id === activeId;
          return (
            <li key={item.id}>
              <a
                href={`#${item.id}`}
                onClick={(event) => handleOutlineClick(event, item.id, scrollTo)}
                className={[
                  "block truncate py-1.5 text-[13px] leading-5 transition-colors",
                  INDENT_CLASSES[depth],
                  active ? "font-medium text-accent" : "text-fg-secondary hover:text-fg",
                ].join(" ")}
              >
                {item.text}
              </a>
            </li>
          );
        })}
      </ul>
    </details>
  );
}
