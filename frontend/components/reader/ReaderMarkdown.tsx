"use client";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";

export function ReaderMarkdown({ children, onPage }: { children: string; onPage?: (page: number) => void }) {
  return <div className="reader-markdown"><ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]}
    rehypePlugins={[[rehypeKatex, { trust: false, strict: "ignore", maxExpand: 1000, maxSize: 15 }]]}
    components={{ a: ({ href, children }) => {
      const page = /^#page=(\d+)$/.exec(href || "");
      if (page && onPage) return <button className="reader-source" onClick={() => onPage(Number(page[1]))}>{children}</button>;
      return <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>;
    }, img: () => <span>〔外部图片未加载〕</span> }}>{children}</ReactMarkdown></div>;
}
