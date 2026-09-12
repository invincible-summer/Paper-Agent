import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "阅研 · Paper Agent — 研究工作台",
  description: "检索文献、阅读原文、记录发现，在同一张研究桌上推进思考与写作。",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh" suppressHydrationWarning>
      <body className="min-h-screen bg-bg text-fg antialiased">
        {children}
      </body>
    </html>
  );
}
