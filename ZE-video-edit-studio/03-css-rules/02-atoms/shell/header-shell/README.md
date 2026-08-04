# header-shell

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

header-shell.css — 工作台顶部固定栏 框架 shell 组件原子(02-atoms · L2)
协议:03-css-rules 契约。消费方:01-core/04-user-facing/01-layout/01-header/(HeaderShell + 子件)。
绝对严格分层(L5 chrome 轮 batch4):顶部栏(.ze-header* 块)是框架级工作台 shell chrome
—— 三段栅格容器、skill 图标/名、项目名/同步指示、菜单 trigger/popover/submenu、主题切换、
头像。含状态化 hover(menu-trigger / theme-toggle / avatar)、data-state 选择器(sync /
avatar)、@keyframes 脉冲动画、popover/submenu 绝对定位,无法降为 L4 无令牌结构,

## 对外表面

`.ze-header`, `.ze-header__acl-add-row`, `.ze-header__acl-error`, `.ze-header__acl-member`, `.ze-header__acl-member-badge`, `.ze-header__acl-member-name`, `.ze-header__acl-panel`, `.ze-header__acl-remove`, `.ze-header__acl-title`, `.ze-header__avatar`, `.ze-header__file-demo-badge`, `.ze-header__file-list`, `.ze-header__file-row`, `.ze-header__file-row-action`, `.ze-header__file-row-actions`, `.ze-header__file-row-check`, `.ze-header__file-row-meta`, `.ze-header__file-row-name`, `.ze-header__file-trigger`, `.ze-header__file-trigger-name`, `.ze-header__icon`, `.ze-header__left`, `.ze-header__menu-caret`, `.ze-header__menu-info-item`
