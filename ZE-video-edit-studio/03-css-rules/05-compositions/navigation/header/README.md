# header

层级：L5 编辑器组合。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

header.css — 工作台顶部固定栏视觉(04-editors · 已迁空 stub)
协议:03-css-rules 契约。
绝对严格分层(L5 chrome 轮 batch4):顶部栏(.ze-header* 块)是框架级工作台 shell chrome
(HeaderShell + LeftSegment / MiddleSegment / RightSegment + menus / controls),含状态化
hover(menu-trigger / theme-toggle / avatar)、data-state 选择器、脉冲动画、popover / submenu
绝对定位,令牌样式无法降为 L4 无令牌结构,已整组上移 L2 组件原子 →

## 对外表面

无 `.ze-*` class；该对象提供令牌、生成物或文档事实。
