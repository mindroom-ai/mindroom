import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

// Get ports from environment variables or use defaults
const backendPort = process.env.BACKEND_PORT || '8765';
const frontendPort = parseInt(process.env.FRONTEND_PORT || '3003');
const isDocker = process.env.DOCKER_CONTAINER === '1';

// Load MINDROOM_API_KEY from the repo-root .env (parent of frontend/).
// The empty prefix '' makes loadEnv read ALL vars, not just VITE_-prefixed ones.
// This key is used server-side by the dev proxy and never reaches the browser.
const rootEnv = loadEnv('development', path.resolve(__dirname, '..'), '');
const apiKey = process.env.MINDROOM_API_KEY || rootEnv.MINDROOM_API_KEY;

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
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
