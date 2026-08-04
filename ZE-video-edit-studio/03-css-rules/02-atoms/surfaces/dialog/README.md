# dialog

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

dialog.css — ze-dialog 组件视觉 (02-atoms) — CSS-only shell
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
视觉 DNA: 对齐 v3 overlay 阴影 + raised 底 + header/body/footer 三段。
两条路径:① 原生 <dialog class="ze-dialog"> + ::backdrop;② 非原生用
[data-open="true"] + 同级 .ze-dialog__backdrop 兜底。

## 对外表面

`.ze-dialog`, `.ze-dialog__backdrop`, `.ze-dialog__body`, `.ze-dialog__footer`, `.ze-dialog__header`, `.ze-dialog__title`
