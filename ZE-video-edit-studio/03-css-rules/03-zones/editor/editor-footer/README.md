# editor-footer

层级：L3 功能区。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

editor-footer.css — footer 功能区(editor 内本地状态条)结构 (03-zones)
zone id 仍为 `footer`,文件名加 `editor-` 前缀以区别于 04-editors/footer.css(工作台级 Footer)。
绝对严格分层(DESIGN-SYSTEM §0):本文件 **零 var(--ze-*)** —— 视觉/排版全走 L2 原子:
容器 = .ze-bar[data-surface=raised][data-edge=bottom];小字 = .ze-text[data-role=tertiary];
段内统计 = status-tile / chip / badge / progress 原子。本文件只留"状态条特有的、无令牌的结构":
段的弹性增长 + 窄容器只留第 1 段。

## 对外表面

`.ze-editor-footer__segment`
