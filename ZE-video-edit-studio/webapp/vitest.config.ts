/**
 * vitest.config — webapp 单测运行器(纯逻辑层)。
 *
 * 覆盖 01-core 的纯函数(layoutOps / selectionModel / 02-interaction)与 webapp/src 内的逻辑。
 * 与 vite.config 同一套 @ze/* alias(core 升级换路径不变)。环境默认 node(纯逻辑无 DOM);
 * 需 DOM 的组件/hook 测试(P4 useInteractionStore)再按需切 jsdom。
 *
 * 注:`.test.ts` 不进 core checksum 指纹(skill_sync 排 `.test.`),也不在 tsc -b 的 include 内。
 */
import { defineConfig } from 'vitest/config'
import path from 'node:path'

export default defineConfig({
  resolve: {
    alias: {
      '@ze/core': path.resolve(__dirname, '../01-core'),
      '@ze/css': path.resolve(__dirname, '../03-css-rules'),
      '@ze/i18n': path.resolve(__dirname, '../05-i18n'),
      '@mod': path.resolve(__dirname, '../02-mod'),
    },
    dedupe: ['dompurify', 'react', 'react-dom'],
  },
  test: {
    include: ['src/**/*.test.ts', '../01-core/**/*.test.ts'],
    environment: 'node',
    // .test.ts 不下发下游(skill_sync 排除),下游空跑不应红;有测时照常跑。
    passWithNoTests: true,
  },
})
