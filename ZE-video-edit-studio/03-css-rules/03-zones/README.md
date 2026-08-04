# 03-zones — 功能区

> 一句话:功能区是 editor 内部按职责划分的横条或内容块;editor 只是把这些功能区组合起来。

> **闭集权威 = [ZONE-CLASS-REGISTRY.md](./ZONE-CLASS-REGISTRY.md)**(8 结构类,四产品实测收敛)。下面的"管理/工具/内容"是这 8 类的组织视角。

## 标准词表

新公共口径只认三类:

- **管理功能区 ManageZone**:搜索、筛选、排序、选择管理、全选、反选、清空选中、删除选中。它负责“看什么、管哪些实体”。
- **工具功能区 ToolZone**:编辑工具、运行按钮、command、pipeline 触发、brush/eraser/select/transform/retry 等。它负责“对当前对象做什么”。
- **内容功能区 Content Zone**:按 editor 命名,例如 Canvas2DZone、Model3DZone、CommandZone、PropertyZone、OutlineZone、CardListZone、HtmlZone、ConversationZone。它负责“当前工作面本身”。

旧 `filterbar/control/toolrow/actionbar/toolbar` 只作为实现兼容层:

| 旧名 | 新口径 |
|---|---|
| `filterbar` | ManageZone 的一种实现 |
| `control` / `toolrow` / `actionbar` | ToolZone 的一种实现 |
| `toolbar` | editor chrome 的历史槽位;操作按钮逐步迁到 ToolZone |
| `inspector` | 旧结构 primitive;公共产品名改为 `property` editor |

## 横条布局规则

ManageZone 和 ToolZone 都是横条,但不是“随便挤一排按钮”:

- `wrap-cluster`:换行成簇,每项 hug 宽,适合 pill 筛选。
- `flex-fill`:换行并占满当前行宽,按钮按 `data-flex` 比例拉伸,靠 min width 自动决定每行数量。
- `hug-scroll`:单行工具条,空间不够时横向滚动。

所有横条必须声明滚动归属、min-width/min-height 和容器断点规则,避免按钮挤压、文字溢出、内容区被撑坏。

## 每类编辑器的组合

| editor | 管理功能区 | 工具功能区 | 内容功能区 |
|---|---|---|---|
| `canvas2d` | 可选 | 是 | Canvas2DZone |
| `scene3d` | 可选 | 是 | Model3DZone |
| `cardlist` | 是 | 是 | CardListZone |
| `property` | 可选 | 是 | PropertyZone |
| `pipeline` | 可选 | 是 | CommandZone |
| `outline` | 是 | 是 | OutlineZone |
| `conversation` | 可选 | 是 | ConversationZone |
| `html` | 可选 | 是 | HtmlZone |

## 怎么用

mod 作者通常不直接写 zone CSS,只在 `02-mod/04-layout.config.ts` 给 slot 配 view_state。core editor 组件把 view_state 翻译成 ManageZone / ToolZone / 内容功能区。

需要新增功能区能力时回 build-v3 源头补:

1. 先补 `zones/types.ts` 契约。
2. 再补 zone 组件或复用现有兼容组件。
3. 更新 `EDITOR_MANIFEST.json`、SPEC、fixture、测试。
4. 下游 skill 只消费新字段,不复制 `.ze-*` CSS。

完整规则见 [SPEC.md](./SPEC.md)。
