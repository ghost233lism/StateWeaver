import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const repositoryRoot = decodeURIComponent(new URL('../..', import.meta.url).pathname)
  const env = {
    ...loadEnv(mode, repositoryRoot, ''),
    ...loadEnv(mode, '.', ''),
  }
  const apiProxyTarget = env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8186'
  const proxy = {
    '/api': {
      target: apiProxyTarget,
      changeOrigin: true,
      ws: true,
    },
  }

  return {
    plugins: [react()],
    server: { proxy },
    preview: { proxy },
  }
})
