import { defineConfig } from 'vite'
import { builtinModules } from 'module'
import path from 'path'
import pkg from './package.json'

// 主进程构建：cjs 输出 dist/main/main.js，Electron 与 Node 内置模块全部 external
// #rename-appid：编译期注入 build.appId 给 main.ts 用（避免硬编码 + runtime 读文件）
export default defineConfig({
  define: {
    // 缺失时硬编码兜底（防 JSON.stringify(undefined) 让 vite 崩）
    __APP_ID__: JSON.stringify(pkg.build?.appId ?? 'com.rong-info.shortvideotool'),
  },
  build: {
    lib: {
      entry: path.resolve(__dirname, 'src/main/main.ts'),
      formats: ['cjs'],
      fileName: () => 'main.js',
    },
    outDir: 'dist/main',
    emptyOutDir: true,
    rollupOptions: {
      external: [
        'electron',
        ...builtinModules,
        ...builtinModules.map((m) => `node:${m}`),
      ],
    },
    target: 'node18',
    minify: false,
  },
})