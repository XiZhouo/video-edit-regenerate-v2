# Table editor validation rules

## 子模块结构

- 必须存在 `TableEditor.tsx`，这是该 editor 的代码入口。
- 必须存在 `README.md`、`CONSUMPTION.md`、`VALIDATION.md`。
- 子模块可以放自己的测试，但共享 primitives/zones/lib 仍留在 `03-editor/` 根层。

## 机器校验

- manifest 必须有 id=table 且 rootSelector 指向 .ze-editor--table
- rowModel=row-h 时所有 rowSelector 必须使用 var(--ze-row-h)
- TableEditor.tsx 不得 import 其他 *Editor.tsx；组合只能发生在 LayoutTree

## 人工审查口径

新增能力先问三件事：它是不是这个 editor 的通用工作面能力；能不能通过 `view_state`/command 接缝表达；如果需要和另一个 editor 同屏，是否应该上移到 L5 LayoutTree，而不是在本 editor 内嵌。
