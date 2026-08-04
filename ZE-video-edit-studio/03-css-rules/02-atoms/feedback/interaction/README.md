# interaction — 微交互族谱 / 选中态 / 动效降级(T5/T6/T8/T9/T10,css 19.9.0)

## 怎么用
- 选中:行/卡/片段挂 `data-selected="true"`(或 `.ze-sel`)= accent 边+wash 底+辉光,全体系唯一配方,禁自创浮动选择框。
- 海拔:raised 卡加 `.ze-card-hi` 获得瓷釉顶缘高光;hover 换描边/底色,不加重阴影。
- 手感:图标按钮 `.ze-press-sm`;≥32px 行卡 `.ze-lift`(26px 密行禁挂——只换底);面板组入场 `.ze-enter` + `.ze-enter-1..4` 错峰;手操件(switch/slider/拖拽)`.ze-springy`;进行中状态灯 `.ze-breathe`。
- 降级:`<html data-motion="off">` 全杀动效;`prefers-reduced-motion` 自动同效(`data-motion="on"` 显式豁免)。
- 缓动分工铁律:standard 给状态/入场;**spring 只给手直接操作的件**。

来源:iOS 基线 13 手感 + ZE前端美学优化规划 T5-T10;时长/keyframes 门禁随美学批2。
