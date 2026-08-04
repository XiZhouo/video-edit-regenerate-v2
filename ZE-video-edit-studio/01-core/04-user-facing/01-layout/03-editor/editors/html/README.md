# Html editor

HTML 内容编辑器(`html` class)。编辑一个 entity 的 HTML 字段,预览/源码双态。text/code 同族的薄内容编辑器。

## 职责

- **预览**:沙盒 iframe(`srcDoc`)渲染 HTML 产物。
- **源码**:textarea 编辑 HTML,blur 写回(CAS)。
- 典型消费者:AI 生成的页面/组件预览(chat-canvas-studio)、教程图文产物(tutorial-writer)。

## 安全(策略住 core,不可 mod 配)

iframe `sandbox="allow-scripts"` 硬编码:产物 HTML 是 AI/用户生成的不可信内容,刻意**不给 `allow-same-origin`**(否则脚本能触达父应用 DOM/cookie)、不给 `allow-popups`。XSS 隔离是受管 core 的职责,mod 不能经 view_state 放宽沙盒。

## 不是什么

- 不是富文本/Markdown 编辑器(那是 `text`)。
- 不是代码编辑器(那是 `code`;html 的源码态只是辅助,主用途是预览渲染产物)。
