import path from "path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { svgzPlugin } from "./svgz.config";

export default defineConfig({
  root: path.resolve(__dirname, "connections"),
  base: "/connections/",
  publicDir: false,
  plugins: [react(), svgzPlugin],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
      "/src": path.resolve(__dirname, "src"),
    },
  },
  build: {
    outDir: path.resolve(__dirname, "dist/connections"),
    emptyOutDir: false,
  },
});
