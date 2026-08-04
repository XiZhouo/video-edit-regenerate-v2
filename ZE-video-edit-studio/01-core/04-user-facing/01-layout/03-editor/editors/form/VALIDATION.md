# Form editor validation rules

## 子模块结构

- 必须存在 `FormEditor.tsx`，这是该 editor 的代码入口。
- 必须存在 `README.md`、`CONSUMPTION.md`、`VALIDATION.md`。
- 子模块可以放自己的测试，但共享 primitives/zones/lib 仍留在 `03-editor/` 根层。

## 机器校验

- manifest 的 nest 声明必须有 unitSelector 和 infoSelector，且 CSS 里存在
- 聚合编辑只能走 entity.batch_update 或声明 command，不能直接 mutate entity
- FormEditor.tsx 仍在 toolbar 迁移 ledger 里；新逻辑不得继续扩散旧 toolbar= 模式

## 人工审查口径

新增能力先问三件事：它是不是这个 editor 的通用工作面能力；能不能通过 `view_state`/command 接缝表达；如果需要和另一个 editor 同屏，是否应该上移到 L5 LayoutTree，而不是在本 editor 内嵌。
