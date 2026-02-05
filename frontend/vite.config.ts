import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

// Get ports from environment variables or use defaults
const backendPort = process.env.BACKEND_PORT || '8765';
const frontendPort = parseInt(process.env.FRONTEND_PORT || '3003');
const isDocker = process.env.DOCKER_CONTAINER === '1';

const monacoPath = path.resolve(__dirname, 'node_modules/monaco-editor/min');

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
      '/monaco': monacoPath,
    },
  },
  server: {
    port: frontendPort,
    // Allow all hosts when running in Docker
    allowedHosts: isDocker ? ['.nijho.lt', '.local', '.mindroom.chat'] : [],
    proxy: {
      '/api': {
        target: `http://localhost:${backendPort}`,
        changeOrigin: true,
      },
    },
    fs: {
      allow: [path.resolve(__dirname), monacoPath],
    },
  },
  optimizeDeps: {
    include: ['monaco-editor/esm/vs/editor/editor.api'],
  },
});
