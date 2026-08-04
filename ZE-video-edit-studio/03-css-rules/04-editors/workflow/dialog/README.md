# dialog

层级：L4 编辑器。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

dialog.css — dialog 编辑器(AI 对话)结构 (04-editors)。消费：01-core 的 DialogEditor（`.ze-editor--dialog`）。transcript 气泡流 + 底部 composer。**零令牌**：视觉/间距全走 L2 原子（气泡 `.ze-surface[data-surface=sunken|accent-wash]` + `.ze-edge` + `.ze-round` + `.ze-pad`，文字 `.ze-text`，间距 `.ze-gap`/`.ze-pad`，由 DialogEditor 贴），本文件只留无令牌结构（flex/滚动/左右对位/换行）。

## 对外表面

`.ze-dialog__transcript`, `.ze-dialog__bubble`, `.ze-dialog__role`, `.ze-dialog__text`, `.ze-dialog__composer`, `.ze-dialog__input`（transcript+composer 直接做 `.ze-editor__content` 的子，无额外容器）
