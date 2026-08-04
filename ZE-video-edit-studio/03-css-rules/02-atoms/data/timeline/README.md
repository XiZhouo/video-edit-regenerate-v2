# timeline

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

timeline.css — ze-timeline 时间轴可视化组件原子 (02-atoms / L2 组件原子)
协议: 03-css-rules/02-atoms/SPEC.md · 总纲 ../../DESIGN-SYSTEM.md(§3 L2)
入口: @ze/css/02-atoms/index.css → @import
为什么在 L2:TimelineZone(框架级内容区 primitive)的时间轴可视化是内聚状态化组件 —— 轨道行
(lane)走行高系统 min-height=row-h、片段(clip)在轨内绝对定位且 选中(`[data-selected]` info
描边)/ 运行态(`[data-run-phase]` 淡出/红边)/ 创建草稿(`[data-draft="true"]` 虚线信息色轮廓、
半透明且不截获指针)分支,clip 底色由片段 data 经内联 `--clip-color`。

`data-draft="true"` 只表达“用户正在拖出、尚未提交的区间”，不代表实体、选择或运行态。调用方只负责
发出事实属性和运行时位置/宽度；透明度、轮廓与事件穿透全部由本原子负责，不能在 TSX 或 mod 里重复定义。

## 对外表面

`.ze-timeline__clip`, `.ze-timeline__clip[data-draft="true"]`, `.ze-timeline__clip-handle`, `.ze-timeline__clip-label`, `.ze-timeline__inner`, `.ze-timeline__lane`, `.ze-timeline__lanes`, `.ze-timeline__playhead`, `.ze-timeline__ruler`, `.ze-timeline__scroll`, `.ze-timeline__tick`
