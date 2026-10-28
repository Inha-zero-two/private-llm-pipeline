import react from '@vitejs/plugin-react'
import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv } from 'vite'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')

  return {
    plugins: [react()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      // /api 요청을 백엔드로 넘긴다 (개발 서버 전용, CORS 회피)
      proxy: {
        '/api': {
          target: env.API_URL || 'http://localhost:8000',
          changeOrigin: true,
        },
      },
    },
  }
})
