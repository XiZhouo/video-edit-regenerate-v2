# kv-list

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

kv-list.css — ze-kv-list 组件视觉 (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
键值检视列表:左 key(次级)右 value(主)。对齐 footer 的 kv 行 / Header about kv。
data-editable 行给可编辑提示(铅笔);mono 变体值用等宽(trace-id / hash 等)。
全部走 令牌 (var(--ze-*));禁止字面量 hex;主题切换仅靠 token。

## 对外表面

`.ze-kv-list`, `.ze-kv-list__key`, `.ze-kv-list__row`, `.ze-kv-list__value`
