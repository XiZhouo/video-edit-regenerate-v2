# app-shell

层级：L5 编辑器组合。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

app-shell.css — 应用最外层框架(`.ze-app` / `.ze-app__main`)
`.ze-app` 是装 Header / 中区 / Footer 的最外层布局区,归 04-editors(皮肤的一部分)。
webapp 入口(webapp/src/main.tsx)link 本文件;`.ze-app*` 类的用法在 webapp/src/App.tsx。
绝对严格分层(L5 chrome 轮 batch2):本文件零 var(--ze-*)。
- 全局 reset + body 文档基线(底色/墨色/字体/行高 + box reset + 整页不滚高度链)已迁 L1
foundation → 见 03-css-rules/01-tokens/base.css(在 tokens.css 后 import)。

## 对外表面

`.ze-app`, `.ze-app__main`
