# Operation manual

`.ze-operation-manual` 是 Build V3 对操作说明 HTML 的唯一视觉所有者。渲染器只会把已经净化、且以 `.ze-om` 为单一根节点的语义 HTML 放进这个容器。

支持标题、段落、列表、引用、代码、表格、图片/音视频、`details`，以及可选的 `.ze-om__callout`。所有颜色、字号、间距、圆角均消费 L1 token；下游手册不得携带 `<style>`、`style` 属性或私有 CSS。
