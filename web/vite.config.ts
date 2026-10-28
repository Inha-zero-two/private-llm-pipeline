import react from '@vitejs/plugin-react'
import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv } from 'vite'

// https://vite.dev/config/
export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, process.cwd(), '')

  // 운영: 웹(CloudFront, https)이 앱 서버를 부르므로 서버 주소도 https여야 한다. 브라우저가 http 요청을 막기 때문.
  // 비워 두면 웹과 같은 주소의 /api (CloudFront에서 /api만 서버로 넘길 때)
  if (command === 'build' && env.VITE_API_MODE === 'http' && env.VITE_API_BASE_URL) {
    if (!env.VITE_API_BASE_URL.startsWith('https://')) {
      throw new Error(
        `배포 빌드의 VITE_API_BASE_URL에는 https:// 주소를 넣어야 합니다. 지금 값: "${env.VITE_API_BASE_URL}"`,
      )
    }
  }

  return {
    plugins: [react()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      // 개발: /api 요청을 로컬에서 띄운 앱 서버로 넘긴다 (CORS 회피)
      proxy: {
        '/api': {
          target: env.API_URL || 'http://localhost:8000',
          changeOrigin: true,
        },
      },
    },
  }
})
