import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "MindRoom - Your AI Agent Platform",
  description: "Deploy AI agents that work across all your communication platforms",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={inter.className}>
      <body className="min-h-screen bg-gradient-to-br from-amber-50 via-orange-50/40 to-yellow-50/50 antialiased">
        {children}
      </body>
    </html>
  );
}
