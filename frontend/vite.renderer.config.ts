import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// 渲染层构建：base './' 保证 file:// 协议加载资源正确
export default defineConfig({
  plugins: [react()],
  root: '.',
  base: './',
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist/renderer',
    emptyOutDir: true,
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src/renderer/src'),
    },
  },
})