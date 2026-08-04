# 状态模型

状态从数据开始，不从视觉开始。后端 entity / run 记录里必须能表达当前对象处于什么状态；前端只负责把这个事实贴到对应 UI 对象上；CSS 负责对象自己怎么显示。

## 数据事实

后端 entity 至少要能给出这类字段：

```ts
type EntityUiState = 'normal' | 'empty' | 'running' | 'error' | 'stale' | 'disabled'

interface EntityStateMark {
  entity_id: string
  entity_type: string
  ui_state: EntityUiState
  run_phase?: 'idle' | 'pending' | 'running' | 'done' | 'error'
  error_code?: string
  error_message?: string
  updated_at: string
}
```

不是每个接口都要逐字长这样，但语义要在。刷新、轮询、WebSocket patch 都应该能精确更新某个 entity 的状态标记。

## 前端映射

前端不猜颜色，只做映射：

- `ui_state` → `data-ui-state`
- `run_phase` → `data-run-phase`
- 字段校验失败 → `aria-invalid="true"`
- 不可操作 → `disabled` 或 `aria-disabled="true"`

列表、表格、卡片、缩略图、字段行都按自己的 entity 贴状态。父容器可以汇总状态，但不能用父容器的“全局 loading”盖掉子对象的精确状态。

## CSS 显示

L1 token 定义状态角色：

- normal：正常可读。
- empty：对象存在但没有内容。
- running：对象正在被计算或生成。
- error：对象对应的处理失败。
- stale：对象数据过期或等待刷新。
- disabled：对象暂不可操作。

L2 UI 对象消费这些 token。状态只能改变前景、背景、描边、透明度、覆盖层、图标/文字可见性，不能改变外盒尺寸。这样同一个列表刷新时不会跳，表格列宽不会抖，缩略图生成前后不会挤开周围内容。

## mod 怎么消费

mod 只需要提供 entity 状态事实，不需要自己写空态或执行中皮肤：

```tsx
<article
  className="ze-thumb"
  data-ui-state={entity.ui_state}
  data-run-phase={entity.run_phase}
>
  ...
</article>
```

如果某个 skill 需要把图层像素拿去做后续计算，它仍然只是 entity 数据和 UI 对象；CSS 不需要一个专门的绘制模式。后续计算怎么消费这个图层，是该 skill 的业务逻辑，不是通用编辑器 core 的视觉模式。

像素层同理。背景层、物体层、粗选范围层、候选结果层都应该从 entity 或字段状态得到 `empty/running/error/stale` 等标记；UI 对象在自己原尺寸内显示这些状态。不要另外造一个全局骨架屏、全局空态或专门的 mask 视觉模式来替代具体对象自己的状态。
