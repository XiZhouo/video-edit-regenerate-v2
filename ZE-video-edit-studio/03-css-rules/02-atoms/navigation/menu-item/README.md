# menu-item

层级：L2 UI。代码入口：`code.css`。

## 说明

menu-item.css — ze-menu-item 组件视觉 (02-atoms)

协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import

通用菜单项(给 popover / dropdown / context-menu 内用):24px 行高 + overlay-soft hover +
accent active + kbd 快捷键 + danger 变体 + ▸ 子菜单标记。Header 的 MenuItem 组件即用本类。
全部走 令牌 (var(--ze-*));禁止字面量 hex;主题切换仅靠 token。

## 对外表面

`.ze-menu-group`, `.ze-menu-item`, `.ze-menu-item__icon`, `.ze-menu-item__shortcut`, `.ze-menu-section-header`
