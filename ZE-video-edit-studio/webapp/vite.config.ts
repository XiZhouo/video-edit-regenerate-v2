import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'
import { existsSync } from 'node:fs'

/**
 * Vite 配置
 *
 * 关键:path alias 把 core / css / mod 抽象成短路径
 *   - `@ze/core/...` → ../01-core/...
 *   - `@ze/css/...`  → ../03-css-rules/...
 *   - `@ze/i18n`     → ../05-i18n(字串词表层)
 *   - `@mod/...`     → ../02-mod/...
 *
 * 好处:core 升级(Skill.Sync 替换 01-core)时,App.tsx 的 import 路径不变。
 */
export default defineConfig({
  plugins: [react()],

  resolve: {
    alias: {
      '@ze/core': path.resolve(__dirname, '../01-core'),
      '@ze/css': path.resolve(__dirname, '../03-css-rules'),
      '@ze/i18n': path.resolve(__dirname, '../05-i18n'),
      '@mod': path.resolve(__dirname, '../02-mod'),
    },
    // core/css/i18n 等源在 webapp 之外(../01-core …),其 `import 'react'` 在 build(rollup)
    // 态从文件所在目录找不到 webapp/node_modules。dedupe 强制所有 react 解析到 webapp 这一份,
    // 修复 "Rollup failed to resolve react/jsx-runtime"(dev 态 vite 从 root 解析故无此问题)。
    dedupe: ['dompurify', 'react', 'react-dom'],
  },

  server: {
    port: 5173,
    strictPort: false,
  },

  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      // 多入口:主应用 + 独立交互式 gallery(设计系统展柜,仅 build-v3 自带)。
      // gallery.html 不随 scaffold/sync 下发(产品 skill 不需展柜)→ 缺它即只 build main,
      // 否则 rollup 报 "Could not resolve entry module gallery.html"、下游产线 build 必挂。
      input: existsSync(path.resolve(__dirname, 'gallery.html'))
        ? { main: path.resolve(__dirname, 'index.html'), gallery: path.resolve(__dirname, 'gallery.html') }
        : { main: path.resolve(__dirname, 'index.html') },
    },
  },
})
