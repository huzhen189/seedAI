import "./globals.css";
import type { ReactNode } from "react";

export const metadata = {
  title: "SeedAI · 对话生成网站",
  description: "用 AI 对话生成网站 / 文档 / 代码",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
