import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')

  return {
    plugins: [react(), tailwindcss()],
    server: {
      port: 5173,
      strictPort: true,
      proxy: {
        '/api': {
          target: env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8010',
          changeOrigin: true,
        },
        // AG-UI runs stream over SSE; ``ws: false`` keeps the response a
        // plain chunked body so the browser can read it incrementally.
        '/agui': {
          target: env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8010',
          changeOrigin: true,
          ws: false,
        },
      },
    },
  }
})
