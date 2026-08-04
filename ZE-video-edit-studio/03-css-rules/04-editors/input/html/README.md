# html

层级：L4 编辑器。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

html.css — html 编辑器内容视觉 (04-editors)。消费：01-core 的 HtmlEditor(`.ze-editor--html`）。预览态 = 沙盒 iframe（`.ze-editor__html-frame`），源码态复用 `.ze-code__textarea`（等宽，见 input/code）。基座在 editor.css。零令牌：只留 iframe 填满 + 去默认边框的无令牌结构；沙盒属性由 React 画死，不在 CSS。

## 对外表面

`.ze-editor__html-frame`
