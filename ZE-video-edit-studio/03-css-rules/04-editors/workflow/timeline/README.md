# timeline

层级：L4 编辑器。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

timeline.css — timeline 编辑器内容视觉(04-editors · 已迁空 stub)
SPEC: 01-core/04-user-facing/01-layout/03-editor/SPEC.md
绝对严格分层(P4 batch12):时间轴可视化(.ze-timeline__scroll/ruler/tick/lane/clip/
clip-label/clip-handle/playhead)是框架级内容区内聚状态化组件 —— 轨道行 row-h 行模型、片段
选中/run-phase 分支、clip 底色经内联 --clip-color + 令牌回退,无法降为 L4 无令牌结构,已整组
上移 L2 组件原子 → 见 03-css-rules/02-atoms/timeline.css(经 index.css @import)。

## 对外表面

无 `.ze-*` class；该对象提供令牌、生成物或文档事实。
