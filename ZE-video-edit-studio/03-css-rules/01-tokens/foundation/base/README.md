# base

层级：L1 token。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

base.css — 文档基线(reset + body 默认外观)(01-tokens / L1 foundation)
SPEC: 03-css-rules/01-tokens/SPEC.md · 总纲 ../DESIGN-SYSTEM.md(§2 L1)
入口: Web 端在 tokens.css 之后一次性 import(main.tsx / gallery/main.tsx)
为什么在 L1:这是「把令牌词表实例化到文档根」—— 与 tokens.css 的 `:root { --ze-* }` 定义令牌
是同一桩 foundation 事的两半:tokens.css 定义词表,base.css 把词表落成文档默认(底色/墨色/
字体/行高 + box reset + 整页不滚的高度链)。它既非可复用组件(L2 原子)、也非组合(L3/4/5),

## 对外表面

无 `.ze-*` class；该对象提供令牌、生成物或文档事实。
