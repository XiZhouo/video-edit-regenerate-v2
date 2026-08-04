# keyframe-curve editor

代码入口: `CurveEditor.tsx`。状态: **candidate**(尚无真实 v3 消费者)。

## 说明

数值×时间曲线面(dope-sheet / curve)。每个关键帧是可独立选中的 entity,映射 time(x)/value(y)/ease。
与 `timeline`(片段编排)并列的第二个时序面:timeline 摆「块」,keyframe-curve 画「值随时间变」。

## Core 负责

- time/value/ease 字段映射;选中、拖动改 time+value(单 entity.update CAS)、双击空白新增、Del 删除
- 自适应值域(或 view_state valueMin/valueMax)、时间缩放、按时间排序连折线
- 共享时间游标 currentTime(秒,Interaction State):注入则画竖直播放头(与 media/timeline 同轴,不入 undo)

## Core 不负责

- 插值算法语义(linear/bezier/物理 bake)、ease 的具体曲线渲染(v1 直线段;留 mod/后续)
- 关键帧属于哪条属性轨、值的业务含义 —— mod 用 entity + command + layout 决定

## 为何独立于 timeline

clip 是「时间上摆一块」,keyframe 是「数值随时间变」(有纵向数值轴 + 段间插值)。强塞进 clip 会让
两种时间粒度混在一个面里(参考 MarketingCreatives 的 2200 行混合体反例)。故按 SPEC C0 做并列的第二条
currentTime 轴消费者,靠 selection + currentTime 与 timeline 联动,互不 import。

## 消费入口

mod 在 LayoutTree 声明 slot,`slotAdapters.tsx` 把 view_state 翻成 props。稳定 import:
`@ze/core/04-user-facing/01-layout/03-editor`。
