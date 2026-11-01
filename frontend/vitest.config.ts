import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { availableParallelism } from "os";
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
    // More workers than cores starve each other into timeouts, and past about 12 threads the main thread serving
    // them chokes.
    maxWorkers: Math.min(12, availableParallelism()),
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
});
