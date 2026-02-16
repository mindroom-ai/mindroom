import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

// Get ports from environment variables or use defaults
const backendPort = process.env.BACKEND_PORT || '8765';
const frontendPort = parseInt(process.env.FRONTEND_PORT || '3003');
const isDocker = process.env.DOCKER_CONTAINER === '1';
// Dashboard API key — injected server-side by the proxy so it never
// appears in the browser JS bundle.  Read from the repo-root .env
// (same var the backend uses).
const apiKey = process.env.MINDROOM_API_KEY;

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  // Load .env from repo root (parent of frontend/) so that
  // MINDROOM_API_KEY set in the root .env is available to the proxy.
  envDir: '..',
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: frontendPort,
    allowedHosts: ['.nijho.lt', '.local', '.mindroom.chat'],
    proxy: {
      '/api': {
        target: `http://localhost:${backendPort}`,
        changeOrigin: true,
        configure(proxy) {
          if (apiKey) {
            proxy.on('proxyReq', (proxyReq, req) => {
              if (!req.headers.authorization) {
                proxyReq.setHeader('Authorization', `Bearer ${apiKey}`);
              }
            });
          }
        },
      },
    },
  },
});
