# Timeline editor

代码入口: `TimelineEditor.tsx`。

## 说明

1D 时序编辑器。把多条实体映射为轨道、开始时间、持续时间和颜色，可拖动、缩放、增删片段。

## Core 负责

- track/start/duration/label/color 字段映射
- 选中片段、双击新增、拖动改 start（跨轨）、左/右把手 trim/resize、Del 删除
- 可配吸附栅格（snap）、时间轴缩放与轨道展示
- `maxTracks` 是轨道数据、拖动命中和 DOM 数量共用的容量边界；默认 64，配错过大值仍会在框架硬上限 512 收敛
- 所有轨号经同一规则变成容量内整数：小数向下取整，负数与非数进首轨，超上限进末轨；脏数据仍可见可拖，拖动提交时会写回规范轨号
- 显式 `dynamicTracks:true` 时按占用轨展示，未达 `maxTracks` 时留一条末尾空轨，满容量后不再增长；默认 false，不改变既有固定轨
- 显式 `rangeCreate:true` 且存在 `addCommand` 时，可在空轨拖出范围创建片段；默认 false
- 共享时间游标 currentTime（秒，Interaction State）：scrub 写、外部改则播放头跟随（与 media 同轴，不入 undo）

拖范围先分别对起点和终点执行 clamp + snap，再判断两端是否不同。两端落在同一吸附点，或在右边界外
继续抖动，都只清理草稿，不派发创建命令。最小时长只负责规范合法几何，不能反过来把点击误判为拖拽。
空数据仍默认显示标准空态；只有启用了 `dynamicTracks` 或 `rangeCreate`、并且确实声明了 `addCommand`，
才展示第一条可操作空轨。

## Core 不负责

- 视频播放、音频波形生成、转码
- 业务上什么叫片段有效或冲突

## 消费入口

mod 不直接把这个组件塞进业务页面；mod 在 LayoutTree 里声明 slot，`slotAdapters.tsx` 把 `view_state` 翻译成 props。稳定 import 仍然是 `@ze/core/04-user-facing/01-layout/03-editor`。
