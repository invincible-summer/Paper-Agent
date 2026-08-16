import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Paper Agent — Literature Survey & Review",
  description: "AI-powered literature survey, review, and research direction advisor.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh" suppressHydrationWarning>
      <body className="min-h-screen bg-bg text-fg antialiased">
        <script dangerouslySetInnerHTML={{
          __html: `(function(){try{var p=new URLSearchParams(location.search);var t=p.get('theme');if(!t){t=localStorage.getItem('paper-agent-theme');}if(!t){t='light';}if(t==='dark'){document.documentElement.classList.add('dark');}}catch(e){}})()`
        }} />
        {children}
      </body>
    </html>
  );
}
