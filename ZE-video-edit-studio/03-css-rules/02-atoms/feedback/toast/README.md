# toast

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

toast.css — ze-toast 组件视觉 (02-atoms) — CSS-only shell
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
瞬时通知条:raised 底 + 左语义条 + 文案 + 可选关闭。toast 层级。
.ze-toast__viewport 是右下角堆叠容器。**出现/自动消失时机 = React 交互层(运行时),本批只给静态视觉**。
全部走 令牌 (var(--ze-*));禁止字面量 hex;主题切换仅靠 token。

## 对外表面

`.ze-toast`, `.ze-toast__body`, `.ze-toast__close`, `.ze-toast__content`, `.ze-toast__icon`, `.ze-toast__title`, `.ze-toast__viewport`
