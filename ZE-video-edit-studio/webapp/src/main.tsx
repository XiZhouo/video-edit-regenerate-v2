/**
 * Web 启动入口
 *
 * 流程:
 *   1. 加载令牌 CSS(必须最先;后续所有 .ze-* 组件依赖)
 *   2. 加载 UI 模块 + 应用外壳 CSS(区域 footer/editor/layout/header 各自被组件内部 import)
 *   3. 挂载 React 到 #root
 *
 * 全部 CSS 都来自 `03-css-rules/`(皮肤,@ze/css)——换肤 = 换那个文件夹;这里只 import。
 */

import React from 'react'
import ReactDOM from 'react-dom/client'

// 令牌 — 必须最先 load
import '@ze/css/01-tokens/tokens.css'
import '@ze/css/01-tokens/themes.css'  // 36 档工具主题色(data-ze-theme;accent 4 值按档覆盖)
import '@ze/css/01-tokens/base.css'
import '@ze/css/01-tokens/icons.css'   // 图标令牌(GENERATED;--ze-icon-*,icon-map.css 消费)

// .ze-* 原子组件库(依赖令牌,故在其后)
import '@ze/css/02-atoms/index.css'

// 应用最外层框架 .ze-app + 全局 reset(归皮肤的 04-editors)
import '@ze/css/05-compositions/app/app-shell/code.css'

import App from './App'

// ── mod 运行时启动钩子(W1-3 建;可选)────────────────────────────────────
// mod 需要在渲染前注册东西(引导锚点、自定义渲染器…)时,建 webapp/src/product/bootstrap.ts
// 并在其顶层执行注册。这里用 eager glob 而不是普通 import:**文件不存在时返回空对象、
// 不报错**,于是本入口对「有没有 bootstrap」两种 mod 都成立。
//
// 为什么不让 mod 直接改 main.tsx:main.tsx 是受管文件,skill_sync 会用上游版覆盖它 ——
// 写在这儿的注册下一次同步就没了(W1-3 施工时实测撞到,foundry 的注册差点被抹掉)。
// product/ 是 mod 自留地,同步不动它。
import.meta.glob('./product/bootstrap.ts', { eager: true })

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
