# form

层级：L4 编辑器。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

form.css — form 编辑器自有结构(04-editors)
SPEC: 01-core/04-user-facing/01-layout/03-editor/SPEC.md
消费: FormEditor / FormZone —— schema 驱动表单内容区外壳(.ze-editor__form-body)。
字段行 primitive(.ze-field-row* —— FormZone + ParamPanel 复用,承载行高系统/多行例外/
run-phase 骨架/响应式)是框架级共享组件,其令牌样式已上移 L2(02-atoms/field-row.css)。
绝对严格分层(P4 batch10):本层零 var(--ze-*)。body 的内距/行间距由 React 贴 L2 原子

## 对外表面

`.ze-editor__actionbar`, `.ze-editor__form-body`, `.ze-editor__unit-preview`
