# card

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

card.css — ze-card 组件视觉 (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
视觉 DNA: 对齐 v3 footer 的 pipeline 卡 —— raised 底 + 1px overlay-soft 边 + raised 阴影,
data-interactive hover 转 accent-wash 边 + floating 阴影。
全部走 令牌 (var(--ze-*));禁止字面量 hex;主题切换仅靠 token。

## 对外表面

`.ze-card`, `.ze-card__body`, `.ze-card__footer`, `.ze-card__header`, `.ze-card__title`

尺寸：`data-size="xs"` 用于编辑器内密集列表/问题卡；`sm` 为普通紧凑卡；`lg` 为更宽松的独立卡。
