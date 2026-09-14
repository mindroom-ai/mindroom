import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";
import { svgzPlugin } from "./svgz.config";

export default defineConfig({
  plugins: [react(), svgzPlugin],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test/setup.ts",
    exclude: [
      "**/node_modules/**",
      "**/dist/**",
      "**/cypress/**",
      "**/.{idea,git,cache,output,temp}/**",
      "**/{karma,rollup,webpack,vite,vitest,jest,ava,babel,nyc,cypress,tsup,build}.config.*",
      "**/tests/e2e/**", // Exclude E2E tests which are Playwright tests
    ],
    pool: "threads",
    maxWorkers: 12,
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
});
