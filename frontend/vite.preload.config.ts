import { defineConfig } from 'vite'
import path from 'path'

// preload 构建：cjs 输出 dist/preload/preload.js，仅 external electron
export default defineConfig({
  build: {
    lib: {
      entry: path.resolve(__dirname, 'src/preload/preload.ts'),
      formats: ['cjs'],
      fileName: () => 'preload.js',
    },
    outDir: 'dist/preload',
    emptyOutDir: true,
    rollupOptions: {
      external: ['electron'],
    },
  },
})
