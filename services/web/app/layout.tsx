import type { Metadata } from "next";
import "./globals.css";
import Nav from "./components/Nav";
import { ChatProvider } from "./components/ChatContext";
import ChatSidebar from "./components/ChatSidebar";
import { ThemeProvider } from "@/components/theme-provider";
import { GoogleAnalytics } from "@next/third-parties/google";

export const metadata: Metadata = {
  title: "知识库",
  description: "个人知识管理与 AI 辅助写作",
  icons: {
    icon: "/logo.svg",
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body className="font-sans antialiased">
        <ThemeProvider
          attribute="class"
          defaultTheme="system"
          enableSystem
          disableTransitionOnChange
        >
          <ChatProvider>
            <Nav />
            <ChatSidebar />
            {children}
          </ChatProvider>
        </ThemeProvider>
        <GoogleAnalytics gaId="G-HR0B9YJW5B" />
      </body>
    </html>
  );
}
