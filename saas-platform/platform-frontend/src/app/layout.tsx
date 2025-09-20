import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { DarkModeProvider } from "@/hooks/useDarkMode";
import { getServerRuntimeConfig, serializeRuntimeConfig } from "@/lib/runtime-config";

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
  const runtimeConfig = getServerRuntimeConfig()
  const serializedConfig = serializeRuntimeConfig(runtimeConfig)

  return (
    <html lang="en" className={inter.className} suppressHydrationWarning>
      <body className="min-h-screen bg-background text-foreground antialiased transition-colors">
        <script
          id="mindroom-runtime-config"
          dangerouslySetInnerHTML={{
            __html: `window.__MINDROOM_CONFIG__=${serializedConfig};`,
          }}
        />
        <DarkModeProvider>
          {children}
        </DarkModeProvider>
      </body>
    </html>
  );
}
