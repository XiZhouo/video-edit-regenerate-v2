# compare

层级：L4 编辑器。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

compare.css — A/B 前后对比图元结构 (04-editors)
SPEC: 01-core/04-user-facing/01-layout/03-editor/SPEC.md
消费: CompareFrame primitive(.ze-compare*);三态 并排 / 滑块 / 叠加。
纯几何逻辑在 03-editor/primitives/compareGeometry.ts。
绝对严格分层(P4):本层零 var(--ze-*)。所有令牌视觉(底色/倒角/边框色/字号/内距/尺寸/偏移)
由 React 贴 L2 原子(.ze-row/.ze-grid/.ze-stack/.ze-pad/.ze-edge/.ze-surface/.ze-round/

## 对外表面

`.ze-compare`, `.ze-compare__body`, `.ze-compare__cell`, `.ze-compare__divider`, `.ze-compare__handle`, `.ze-compare__img`, `.ze-compare__layer`, `.ze-compare__layer--after`, `.ze-compare__opacity`, `.ze-compare__opacity-label`, `.ze-compare__pair`, `.ze-compare__stage`, `.ze-compare__tag`, `.ze-compare__toolbar`
