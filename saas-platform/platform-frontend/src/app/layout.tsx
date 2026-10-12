import type { Metadata } from "next";
import { Bricolage_Grotesque, Hanken_Grotesk, JetBrains_Mono } from "next/font/google";
import Script from "next/script";
import "./globals.css";
import { PlausibleAnalytics } from "@/components/PlausibleAnalytics";
import { DarkModeProvider } from "@/hooks/useDarkMode";
import { getServerRuntimeConfig, serializeRuntimeConfig } from "@/lib/runtime-config";

const hankenGrotesk = Hanken_Grotesk({ subsets: ["latin"], variable: "--font-hanken-grotesk" });
const bricolageGrotesque = Bricolage_Grotesque({ subsets: ["latin"], variable: "--font-bricolage-grotesque" });
const jetbrainsMono = JetBrains_Mono({ weight: "500", subsets: ["latin"], variable: "--font-jetbrains-mono" });

const title = "MindRoom: open-source AI agents in a chat app you can self-host";
const description =
  "AI agents that know you and your work, in a chat app anyone can use. Open source (Apache 2.0), any model, local or cloud. Run it yourself or let us host it.";

export const metadata: Metadata = {
  metadataBase: new URL("https://app.mindroom.chat"),
  title,
  description,
  openGraph: { title, description, siteName: "MindRoom", type: "website" },
  twitter: { card: "summary_large_image", title, description },
};

export const dynamic = "force-dynamic";
export const revalidate = 0;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  const runtimeConfig = getServerRuntimeConfig({ requireSupabase: false })
  const serializedConfig = serializeRuntimeConfig(runtimeConfig)

  return (
    <html lang="en" className={`${hankenGrotesk.variable} ${bricolageGrotesque.variable} ${jetbrainsMono.variable}`} suppressHydrationWarning>
      <body className="min-h-screen bg-background text-foreground antialiased transition-colors">
        <Script
          id="mindroom-runtime-config"
          strategy="beforeInteractive"
          dangerouslySetInnerHTML={{
            __html: `window.__MINDROOM_CONFIG__=${serializedConfig};`,
          }}
        />
        <DarkModeProvider>
          {children}
        </DarkModeProvider>
        <PlausibleAnalytics />
      </body>
    </html>
  );
}
