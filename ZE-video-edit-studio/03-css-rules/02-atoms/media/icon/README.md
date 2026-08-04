# icon

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

icon.css — ze-icon 图标原子 (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / icons.md(语义映射单一真源 + 迭代规则)
入口: @ze/css/02-atoms/index.css → @import
图标 = **令牌**(token 化图库):美术源 icons.svg(16×16 · 1.5px 圆头描边)经
scripts/icons_build.py 生成 01-tokens/icons.css(`--ze-icon-<name>` data URI 令牌)+
同目录 icon-map.css(`.ze-icon[data-icon=<name>]` → mask-image: var(令牌))。

## 对外表面

`.ze-icon`
