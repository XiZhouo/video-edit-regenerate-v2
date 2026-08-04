# media

层级：L4 编辑器。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

media.css — media 编辑器内容视觉 (04-editors)
SPEC: 01-core/04-user-facing/01-layout/03-editor/SPEC.md
消费: 01-core 的 MediaEditor(.ze-editor--media;视频/音频播放器 + 底部传输条)。
共享 body 脚手架(.ze-editor__canvas-body)在 editor.css。
绝对严格分层(P4 去令牌):本文件 **零令牌(无 var())** —— 边框/底色/倒角/内距/时码等宽 全部由
React 贴 L2 实用原子(.ze-bar/.ze-surface/.ze-round/.ze-text[data-mono]/.ze-pad)承载;

## 对外表面

`.ze-media__controls`, `.ze-media__el`, `.ze-media__mark-region`, `.ze-media__marks`, `.ze-media__player`, `.ze-media__scrubber`, `.ze-media__scrubber-wrap`, `.ze-media__time`
