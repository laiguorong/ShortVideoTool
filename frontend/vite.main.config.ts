import { defineConfig } from 'vite'
import { builtinModules } from 'module'
import path from 'path'

// 主进程构建：cjs 输出 dist/main/main.js，Electron 与 Node 内置模块全部 external
export default defineConfig({
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
