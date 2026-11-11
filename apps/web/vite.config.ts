import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '')
  const target = env.VITE_DEV_API_TARGET || 'http://localhost:8000'
  const proxy = {
    '/live': { target, changeOrigin: true },
    '/ready': { target, changeOrigin: true },
    '/api': { target, changeOrigin: true },
  }

  return {
    plugins: [vue()],
    server: {
      host: '0.0.0.0',
      port: 5173,
      proxy,
    },
    preview: {
      host: '0.0.0.0',
      port: 4173,
      proxy,
    },
    build: {
      outDir: 'dist',
      sourcemap: false,
    },
  }
})
