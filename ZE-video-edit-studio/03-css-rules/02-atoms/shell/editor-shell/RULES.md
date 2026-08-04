# editor-shell 规则

- 代码只写在本目录的 `code.css`，不要把对象规则散回层级根目录。
- 状态必须挂在对象自己身上：优先使用 `data-ui-state`、`data-run-phase`、`aria-invalid`、`disabled` 这类事实属性。
- 同一对象的 normal / empty / running / error / stale / disabled 不能改变外盒尺寸；只改变颜色、描边、透明度、覆盖层或内容可见性。
- L2 UI 可以消费 L1 token；L3/L4/L5 只能组合 L2 对象和结构，不重新定义原子视觉。

## 容器角色义务表（01-layout SPEC §2.10 C2b/C2c 的逐 class 落地）

| class | 角色 | 义务 |
|---|---|---|
| `.ze-editor__header` / `.ze-editor__toolbar` / `.ze-editor__statusbar` | chrome-fixed | `flex: 0 0 auto`；行高锁/最小行高 |
| `.ze-editor__work` | fill（分发层） | `flex: 1; min-height: 0`；**永不加 overflow** |
| `.ze-editor__zone`（含 `> .ze-row` / `> .ze-editor__filtergroup`） | hug | `flex: 0 0 auto`；`hug-scroll` 只横滚，`wrap-cluster` 3 行预算内自滚 |
| `.ze-editor__content` | fill；`[data-content-scroll="true"]` 才是 scroll-owner | 默认 `overflow: hidden`；已有内部 owner 的编辑器禁开 contentScroll |
| `.ze-editor__unit` | hug | `flex: 0 0 auto` 与 `overflow: hidden` 成对锁 |
| `.ze-editor__after` | hug / share | hug=`flex: 0 0 auto`；share=`flex: 1 1 0`+min-height 地板，内部自滚 |
| `.ze-editor__canvas-body` | viewport-clip | `overflow: hidden`；禁滚动 fade |
