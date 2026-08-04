/**
 * mod 自己的单测运行器 —— 只跑 02-mod 下的 `.test.ts`。
 *
 * 为什么不并进 webapp/vitest.config.ts:那份是脚手架(四个 skill 同一份),
 * 在一个 skill 里改它会让四家分叉,而受管层的改动按纪律要回流上游 —— 为一条
 * mod 测试动上游不划算。mod 自带一份配置,谁都不碍着谁。
 *
 * 跑法(在 webapp 目录下,node_modules 在那儿):
 *   npx vitest run --config ../02-mod/05-code/renderers/vitest.config.ts
 */
import path from 'node:path'

const SKILL = path.resolve(__dirname, '../../..')

// 不 import vitest/config:这份配置在 webapp 之外,解析不到那儿的 node_modules。
// defineConfig 本来也只是类型标注,导出普通对象等价。
export default ({
  resolve: {
    alias: {
      '@ze/core': path.resolve(SKILL, '01-core'),
      '@ze/css': path.resolve(SKILL, '03-css-rules'),
      '@ze/i18n': path.resolve(SKILL, '05-i18n'),
      '@mod': path.resolve(SKILL, '02-mod'),
    },
    dedupe: ['react', 'react-dom'],
  },
  test: {
    root: SKILL,
    include: ['02-mod/**/*.test.ts', '02-mod/**/*.test.tsx'],
    environment: 'node',
    passWithNoTests: true,
  },
})
