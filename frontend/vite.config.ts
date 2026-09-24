import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import type { IncomingMessage } from "http";
import path from "path";
import { svgzPlugin } from "./svgz.config";

// Get ports from environment variables or use defaults
const mindroomPort = process.env.MINDROOM_PORT || "8765";
const frontendPort = parseInt(process.env.FRONTEND_PORT || "3003");

// Load MINDROOM_API_KEY from the repo-root .env (parent of frontend/).
// The empty prefix '' makes loadEnv read ALL vars, not just VITE_-prefixed ones.
// This key is used server-side by the dev proxy and never reaches the browser.
const rootEnv = loadEnv("development", path.resolve(__dirname, ".."), "");
const userEnvDir = process.env.HOME
  ? path.join(process.env.HOME, ".mindroom")
  : "";
const userEnv = userEnvDir ? loadEnv("development", userEnvDir, "") : {};
const apiKey =
  process.env.MINDROOM_API_KEY ||
  rootEnv.MINDROOM_API_KEY ||
  userEnv.MINDROOM_API_KEY;

// The proxy acts for the operator with that key, so it only attaches it to direct
// loopback connections (no tunnel or reverse proxy forwarding) that name the dev
// server by a loopback host (defeating DNS rebinding) and carry no cross-site
// fetch metadata (defeating other browser tabs). Framing is denied below so another
// site cannot drive the dashboard from an iframe that passes as same-origin.
const loopbackHost = /^(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$/;

function isOperatorRequest(req: IncomingMessage): boolean {
  const peer = req.socket.remoteAddress ?? "";
  const host = req.headers.host ?? "";
  const fetchSite = req.headers["sec-fetch-site"];
  const origin = req.headers.origin;
  return (
    (peer === "::1" ||
      peer.startsWith("127.") ||
      peer.startsWith("::ffff:127.")) &&
    req.headers["x-forwarded-for"] === undefined &&
    req.headers.forwarded === undefined &&
    loopbackHost.test(host) &&
    (fetchSite === undefined ||
      fetchSite === "same-origin" ||
      fetchSite === "none") &&
    (origin === undefined || origin === `http://${host}`)
  );
}

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react(), svgzPlugin],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: frontendPort,
    allowedHosts: [".nijho.lt", ".local", ".mindroom.chat"],
    headers: {
      "X-Frame-Options": "DENY",
      "Content-Security-Policy": "frame-ancestors 'none'",
    },
    proxy: {
      "/api": {
        target: `http://localhost:${mindroomPort}`,
        changeOrigin: true,
        configure(proxy) {
          if (apiKey) {
            proxy.on("proxyReq", (proxyReq, req) => {
              if (!req.headers.authorization && isOperatorRequest(req)) {
                proxyReq.setHeader("Authorization", `Bearer ${apiKey}`);
                // The proxy is now the API client, so drop the browser's origin
                // metadata instead of forwarding the dev server's origin upstream.
                proxyReq.removeHeader("Origin");
                proxyReq.removeHeader("Sec-Fetch-Site");
              }
            });
          }
        },
      },
    },
  },
});
