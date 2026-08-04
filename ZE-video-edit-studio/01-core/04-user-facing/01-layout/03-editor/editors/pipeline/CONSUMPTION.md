# Pipeline editor consumption rules

## Layout 声明

```ts
slot('pipeline', 'pipeline', ['candidate'], { startCommandId: 'shot.batch_generate', resultField: 'image', entityUi: { labelField: 'caption', thumbField: 'image', statusAnimation: 'thumbnail-pulse' } })
```

## `view_state` 消费规则

- startCommandId/pipelineId: 固定当前工作台服务的管线
- runIdField/itemIdField/pipelineIdField/resultField/historyKeyField: entity 与 run 的映射
- allowRunSwitch: 只有调试/历史浏览时才打开
- entityUi: labelField/thumbField/resultField/statusAnimation/summaryFields
- startArgs/argsFromSelection: start cell 参数默认值和选中实体字段注入
- cells: 每元素候选运行 cell 的 input/output/runCommand/runTargetField 映射

## 组合逻辑

pipeline 通常和 gallery 同屏：gallery 看候选/结果，pipeline 看当前候选背后的运行过程。

## 写入规则

所有写入都必须经过 `dispatch(commandId, payload)`。如果某个字段能回退到内置 `entity.update/entity.delete`，也仍然要带 `type/id/expected_version`，让 undo、CAS、运行态和审计链路保持一致。
