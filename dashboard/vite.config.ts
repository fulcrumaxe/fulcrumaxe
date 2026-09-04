import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
  server: {
    port: 5173,
    proxy: {
      // Forward dashboard API calls to the Python backend/api.py on 18099.
      // This lets the React SPA talk to the real autonomous-team state
      // (registry, budget, KPI, agents, loop health, module health).
      // Port can be overridden via AF_API_PORT env var (e.g. for projectb on 5202).
      '/api': `http://localhost:${process.env.AF_API_PORT ?? '18099'}`,
      '/health': `http://localhost:${process.env.AF_API_PORT ?? '18099'}`,
      // Rust saas-service for GitHub OAuth (the only endpoint it actually serves).
      '/auth': 'http://localhost:3000',
      // Forward JSON-RPC calls to backend/server.py HTTP adapter on 8765.
      // Port can be overridden via AF_RPC_PORT env var (read by start-dashboard.sh).
      '/rpc': {
        target: `http://localhost:${process.env.AF_RPC_PORT ?? '8765'}`,
        changeOrigin: false,
      },
    },
  },
})
