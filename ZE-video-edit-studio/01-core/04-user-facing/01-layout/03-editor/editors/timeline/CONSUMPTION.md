# Timeline editor consumption rules

## Layout 声明

```ts
slot('timeline', 'timeline', ['clip'], {
  trackField: 'track', startField: 'start', durationField: 'duration',
  labelField: 'label', colorField: 'color',
  dynamicTracks: true, rangeCreate: true, maxTracks: 64,
  addCommand: 'clip.add',
})
```

## `view_state` 消费规则

- trackField/startField/durationField: 核心字段，缺省分别是 track/start/duration
- labelField/colorField: 视觉字段
- tracks: 固定轨道数量；`dynamicTracks` 未启用时优先使用，但不得突破 `maxTracks`
- maxTracks: 可选容量声明，同时约束数据轨号、拖动命中和实际 lane DOM。缺省 64，有效声明向下取整，非正/非有限回退 64，框架硬上限 512
- dynamicTracks: 严格 opt-in，缺省 false。启用后忽略固定 `tracks`，按当前最大占用轨展示；容量未满时留一条末尾空轨，满 `maxTracks` 后不再生长
- rangeCreate: 严格 opt-in，缺省 false。仅在同时存在 `addCommand` 时允许空轨拖拽创建范围
- snap: 吸附栅格（单位），缺省 0.5（半单位）；1=整单位、`1/fps`=帧、0=关闭吸附
- unitsPerSecond（或 fps）: unit↔秒换算率，缺省 1。共享 `currentTime` 规范单位是秒，本字段把抽象 unit 映射成秒（unit=帧时填 fps、unit=秒时填 1）
- addCommand/updateCommand/deleteCommand: 增改删命令；update/delete 可回退内置命令

## 交互

- 选 / 拖片段体改 start（可跨轨）/ 拖**右**把手改 duration / 拖**左**把手 trim（右缘固定，start+duration 一并改，单 Operation 原子撤销）/ 双击空轨 `addCommand` / 可选拖范围新增 / 拖 ruler 移播放头。
- 拖范围的创建判据是 clamp + snap 后的两个端点确实不同；同一吸附点内抖动、越过右边界继续拖动都不创建。draft 只输出 `data-draft=true` 和 left/width 几何，视觉完全由受管 CSS 负责。
- 空数据默认继续显示标准空态。只有 `addCommand` 存在，并且 `dynamicTracks` 或 `rangeCreate` 至少一个显式启用时，才渲染第一条可操作空轨。
- 历史数据的小数、负数、NaN/缺失或超大轨号不会被 lane 过滤静默丢掉；它们依次收敛为向下取整、首轨或末轨，并在下一次拖动提交时规范化写回。
- 左把手只动 timeline 占位（start/duration）。若片段是「源媒体的窗口」（需要 head trim 改源 in 点），由 mod 另声明 `sourceIn` 字段 + 专用 trim 命令承接，generic 面不内置源 in/out。

## 组合逻辑

timeline 常与 media 同屏：两者经 Interaction State 的共享 `currentTime`（秒）联动 —— 拖 timeline ruler → media seek；media 播放 → timeline 播放头跟随（不入 undo）。form 改选中片段属性。装配层未接共享轴时退回本地 view-only 播放头。三者互不 import，只靠 `selection[clip]` + `currentTime`。

## 写入规则

所有写入都必须经过 `dispatch(commandId, payload)`。新增、拖动和拖范围三条路径都只能派发容量内整数轨号。如果某个字段能回退到内置 `entity.update/entity.delete`，也仍然要带 `type/id/expected_version`，让 undo、CAS、运行态和审计链路保持一致。
