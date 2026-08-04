# footer-shell

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

footer-shell.css — 工作台底部固定栏 框架 shell 组件原子(02-atoms · L2)
协议:03-css-rules 契约。消费方:01-core/04-user-facing/01-layout/02-footer/(FooterShell + 子件)。
绝对严格分层(L5 chrome 轮 batch5,收官):底部栏(.ze-footer* 块)是框架级工作台 shell chrome
—— 折叠态 segment 行(状态/同步/进度/错误/trace)+ 拖拽把手/resizer + 展开主体 section
(op log / pipelines / details tabs / 错误列表 / trace 链)。含状态化 hover(handle / segment /
oplog-entry / pipeline / error-entry)、大量 data-state/data-source/data-level 选择器、

## 对外表面

`.ze-footer`, `.ze-footer__bar`, `.ze-footer__dot`, `.ze-footer__empty`, `.ze-footer__error-entry`, `.ze-footer__error-level`, `.ze-footer__error-message`, `.ze-footer__error-time`, `.ze-footer__error-trace`, `.ze-footer__errors`, `.ze-footer__expand-btn`, `.ze-footer__expanded`, `.ze-footer__handle`, `.ze-footer__op`, `.ze-footer__oplog`, `.ze-footer__oplog-duration`, `.ze-footer__oplog-entry`, `.ze-footer__oplog-label`, `.ze-footer__oplog-source`, `.ze-footer__oplog-status`, `.ze-footer__oplog-time`, `.ze-footer__pipeline`, `.ze-footer__pipeline-artifact`, `.ze-footer__pipeline-artifact-item`
