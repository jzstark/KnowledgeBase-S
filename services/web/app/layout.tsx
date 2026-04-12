import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "知识库",
  description: "个人知识管理与 AI 辅助写作",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="bg-gray-50 text-gray-900 antialiased">{children}</body>
    </html>
  );
}
