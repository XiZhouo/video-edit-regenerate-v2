# Task editor validation rules

## 子模块结构

- 必须存在 `TaskEditor.tsx`，这是该 editor 的代码入口。
- 必须存在 `README.md`、`CONSUMPTION.md`、`VALIDATION.md`。
- 子模块可以放自己的测试，但共享 primitives/zones/lib 仍留在 `03-editor/` 根层。

## 机器校验

- rowSelector 必须沿用 row-h 行高系统
- run 行为只 dispatch command，不在 editor 内启动异步工作
- TaskEditor.tsx 不得直接依赖 pipeline 数据结构

## 人工审查口径

新增能力先问三件事：它是不是这个 editor 的通用工作面能力；能不能通过 `view_state`/command 接缝表达；如果需要和另一个 editor 同屏，是否应该上移到 L5 LayoutTree，而不是在本 editor 内嵌。
