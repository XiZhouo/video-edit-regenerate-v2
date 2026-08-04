# Timeline editor validation rules

## 子模块结构

- 必须存在 `TimelineEditor.tsx`，这是该 editor 的代码入口。
- 必须存在 `README.md`、`CONSUMPTION.md`、`VALIDATION.md`。
- 子模块可以放自己的测试，但共享 primitives/zones/lib 仍留在 `03-editor/` 根层。

## 机器校验

- 拖动更新必须带 expected_version，保证 CAS/undo 链路
- tracks 应是数字，字段映射必须落在 entity.data
- `maxTracks` 缺省必须是有限值 64；声明值必须收敛到正整数且不高于硬上限 512，fixed/dynamic 模式的 lane DOM 都不得超限
- 渲染、跨轨拖动、双击新增和拖范围新增必须共用同一轨号规范器；小数、负数、非有限和超大历史值不得因 lane equality 静默消失
- 产品若把轨道容量写入持久 schema，必须同时处理旧项目迁移和历史重放；不可消费的旧轨号字段应 opt-in `x-ze-history-replay-guard: true`，不能只靠 UI clamp 掩盖磁盘越界值
- dynamic 模式仅在未达 `maxTracks` 时添加末尾空轨；满容量后 lane 数不得继续增长
- dynamicTracks/rangeCreate 必须只接受显式 `true`，缺省行为为 false
- rangeCreate 必须同时有 addCommand；创建有效性只比较 clamp + snap 后的两端，不得用最小时长或像素抖动反推
- `data-draft=true` 的 JSX 只允许 left/width 几何 inline style，颜色、透明度、描边和命中规则归受管 CSS
- TimelineEditor.tsx 不得内嵌 media editor

## 人工审查口径

新增能力先问三件事：它是不是这个 editor 的通用工作面能力；能不能通过 `view_state`/command 接缝表达；如果需要和另一个 editor 同屏，是否应该上移到 L5 LayoutTree，而不是在本 editor 内嵌。
