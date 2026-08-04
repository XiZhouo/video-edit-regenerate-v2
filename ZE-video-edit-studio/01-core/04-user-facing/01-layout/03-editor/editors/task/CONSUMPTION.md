# Task editor consumption rules

## Layout 声明

```ts
slot('jobs', 'task', ['render_job'], { runCommand: 'render_job.run', stagesField: 'stages' })
```

## `view_state` 消费规则

- runCommand: 对当前任务实体派发运行/重跑命令
- stagesField: 从 entity.data 读取阶段数组；缺省时只显示状态
- statusOf: 由 SlotContext 提供，不由 task 自己存运行态

## 组合逻辑

task 适合做批量任务队列；选中某任务后用 form/review/media 展示详情或结果。

## 写入规则

所有写入都必须经过 `dispatch(commandId, payload)`。如果某个字段能回退到内置 `entity.update/entity.delete`，也仍然要带 `type/id/expected_version`，让 undo、CAS、运行态和审计链路保持一致。
