import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

// Get ports from environment variables or use defaults
const backendPort = process.env.BACKEND_PORT || '8765';
const frontendPort = parseInt(process.env.FRONTEND_PORT || '3003');
const isDocker = process.env.DOCKER_CONTAINER === '1';

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
    // Allow all hosts when running in Docker
    allowedHosts: isDocker ? ['.nijho.lt', '.local', '.mindroom.chat'] : [],
    proxy: {
      '/api': {
        target: `http://localhost:${backendPort}`,
        changeOrigin: true,
      },
    },
  },
});
