# Graph editor validation rules

## 子模块结构

- 必须存在 `GraphEditor.tsx`，这是该 editor 的代码入口。
- 必须存在 `README.md`、`CONSUMPTION.md`、`VALIDATION.md`。
- 子模块可以放自己的测试，但共享 primitives/zones/lib 仍留在 `03-editor/` 根层。

## 机器校验

- GraphEditor.tsx 不能嵌 pipeline editor；执行状态应由 statusOf 或 pipeline 独立展示
- 拖动更新必须走 command 并带 expected_version
- depsField 应是稳定 id 列表，不能存 React 状态

## 人工审查口径

新增能力先问三件事：它是不是这个 editor 的通用工作面能力；能不能通过 `view_state`/command 接缝表达；如果需要和另一个 editor 同屏，是否应该上移到 L5 LayoutTree，而不是在本 editor 内嵌。
