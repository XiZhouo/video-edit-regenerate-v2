# keyframe-curve editor consumption rules

> 状态 candidate:消费者(skeletal-animation / motion-spec-builder / controlled-image-generate)接 UI 后转 adopted + 补 fixture。

## Layout 声明

```ts
slot('curve', 'keyframe-curve', ['keyframe'], {
  timeField: 'time', valueField: 'value', easeField: 'ease',
  addCommand: 'keyframe.add', updateCommand: 'keyframe.update', deleteCommand: 'keyframe.delete',
  // 可选:固定值域(否则按数据自适应)+ 时间↔秒换算(接共享 currentTime)
  valueMin: 0, valueMax: 1, unitsPerSecond: 30, snap: 0.5,
})
```

## `view_state` 消费规则

- timeField/valueField: 核心字段(x=时间、y=数值),缺省 time/value
- easeField: 可选,缓动名(v1 仅存储,曲线按直线段渲;插值渲染留后续)
- valueMin/valueMax: 固定纵轴值域;缺省按数据 value 自适应
- snap: 时间吸附栅格(单位),缺省 0.5;`1/fps`=帧、0=关闭
- unitsPerSecond（或随 mod 文档约定）: unit↔秒换算,接共享 currentTime
- addCommand/updateCommand/deleteCommand: 增改删;update/delete 可回退内置 entity.update/delete

## 关键帧 = 实体(规范化)

每个关键帧是一个 entity(按 ID 规范化),增/删/移/改插值都走 command→Operation→CAS。**不要**把关键帧
塞成某 clip 的内嵌数组 —— 那样会丢掉 version/CAS/undo/审计。若关键帧从属某对象/属性,在关键帧 entity
上存 parentId/prop 字段,由 mod 的 selector 归组。

## 组合逻辑

curve 常与 timeline / media 同屏:三者经 Interaction State 的共享 currentTime(秒)联动(scrub 一处、
余者跟随,不入 undo)。互不 import,只靠 selection + currentTime。

## 写入规则

所有写入经 `dispatch(commandId, payload)`,带 `type/id/expected_version`,保 undo/CAS/审计一致。
