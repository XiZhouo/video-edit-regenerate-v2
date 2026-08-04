# graph

层级：L4 编辑器。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

graph.css — graph 编辑器内容视觉 (04-editors)
SPEC: 01-core/04-user-facing/01-layout/03-editor/SPEC.md
消费: 01-core 的 GraphEditor → GraphCanvasZone(.ze-editor--graph)。基座在 editor.css。
绝对严格分层(P4 去令牌):本文件 **零令牌**。点阵底=`.ze-dotgrid`;节点边框/倒角/底色/字/
内距/阴影=`.ze-edge[data-tone=strong]`+`.ze-round`+`.ze-surface`+`.ze-text`+`.ze-pad`+`.ze-elevate[data-elev=sm]`;
选中描边=`.ze-selectable`;运行态=`.ze-runstate`;连边走 currentColor(色由 svg 贴 .ze-text[data-role=tertiary] 给)。

## 对外表面

`.ze-graph__canvas`, `.ze-graph__edge`, `.ze-graph__edges`, `.ze-graph__node`, `.ze-graph__node-label`
