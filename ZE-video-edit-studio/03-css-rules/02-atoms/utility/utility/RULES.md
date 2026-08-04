# utility 规则

- 代码只写在本目录的 `code.css`，不要把对象规则散回层级根目录。
- 状态必须挂在对象自己身上：优先使用 `data-ui-state`、`data-run-phase`、`aria-invalid`、`disabled` 这类事实属性。
- 同一对象的 normal / empty / running / error / stale / disabled 不能改变外盒尺寸；只改变颜色、描边、透明度、覆盖层或内容可见性。
- L2 UI 可以消费 L1 token；L3/L4/L5 只能组合 L2 对象和结构，不重新定义原子视觉。
- `ze-scroll-fade-*` 只用于软滚动内容流（卡片、表单、文本、日志、对话）；画布、媒体预览、3D、graph、timeline 这类硬 viewport 不使用 fade。
- `contentMode="pass-through"` 的父 content 不统一套 fade；内部若有列表/表单等内容流,在那个真实滚动盒上声明 `ze-scroll-fade-*`。
- `wrap-cluster` 功能区只是控件换行,不默认做顶部淡出；不要把状态标注行、筛选行当成长内容流处理。
- `ze-scrollbar` 只管滚动条厚度与低对比可见性；软边仍由 `ze-scroll-fade-*` 决定，硬 viewport 仍靠 viewport 边界裁切。
