# pipeline

层级：L4 编辑器。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

管线内容 = 命令功能区块(每块自带 line-mid 边 + raised 海拔)。EditorFrame 给所有编辑器统一裹的
.ze-editor__content sunken 外框(画布/表单要它当工作台面)对管线是冗余 —— 块外再套一层灰盒 =
多一层嵌套。这里把它透明直通,让命令功能区块**直接坐**编辑器工作区(编辑器 > 多个 command 功能区)。

## 对外表面

`.ze-editor--pipeline`, `.ze-editor__body`, `.ze-editor__content`, `.ze-editor__unit-info`, `.ze-pipeline`, `.ze-pipeline__badge`, `.ze-pipeline__cell`, `.ze-pipeline__cell-actions`, `.ze-pipeline__cell-head`, `.ze-pipeline__cell-headline`, `.ze-pipeline__cells`, `.ze-pipeline__chips`, `.ze-pipeline__desc`, `.ze-pipeline__entity`, `.ze-pipeline__entity-main`, `.ze-pipeline__entity-thumb`, `.ze-pipeline__image`, `.ze-pipeline__io`, `.ze-pipeline__io-arrow`, `.ze-pipeline__io-col`, `.ze-pipeline__io-empty`, `.ze-pipeline__io-head`, `.ze-pipeline__io-img`, `.ze-pipeline__json`
