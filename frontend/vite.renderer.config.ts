import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'
// 读取 package.json 中的版本号，注入为 __APP_VERSION__ 全局常量
// renderer 层通过这个常量显示在 AboutDialog / 底栏（替代 App.tsx 硬编码的 v1.0.0）
// 后续 npm version 升版本后自动同步，无需手动维护多处
import pkg from './package.json'

// 渲染层构建：base './' 保证 file:// 协议加载资源正确
export default defineConfig({
  // 自定义全局常量（vite 编译期替换）
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
  },
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