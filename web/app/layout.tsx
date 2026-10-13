import type { Metadata } from "next";
import { Be_Vietnam_Pro, IBM_Plex_Mono, Sora } from "next/font/google";
import { BRAND } from "@/lib/site";
import "./globals.css";

const sans = Be_Vietnam_Pro({ subsets: ["latin", "vietnamese"], weight: ["400", "500", "600", "700"], style: ["normal", "italic"], variable: "--font-sans", display: "swap" });
// Figures and tickers: a monospace with full Vietnamese support.
// The "AgentFinX" wordmark only (Sora has no Vietnamese glyphs, so nothing else uses it).
const logo = Sora({ subsets: ["latin"], weight: ["600"], variable: "--font-logo", display: "swap" });
const mono = IBM_Plex_Mono({ subsets: ["latin", "vietnamese"], weight: ["400", "500"], variable: "--font-mono", display: "swap" });

export const metadata: Metadata = {
  title: { default: `${BRAND} | Hệ thống phân tích báo cáo tài chính`, template: `%s | ${BRAND}` },
  description: "Phân tích báo cáo tài chính cùng AI agent: trích xuất số liệu, so sánh chỉ số và trả lời bằng tiếng Việt kèm nguồn trong báo cáo.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="vi" className={`${sans.variable} ${mono.variable} ${logo.variable}`}>
      <body>{children}</body>
    </html>
  );
}
