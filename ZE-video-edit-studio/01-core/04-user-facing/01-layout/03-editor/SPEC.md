# Editor — 中区渲染单元

> **5 层体系 · L4 编辑器 / Editors** — 只引用 L3 功能区(EditorFrame + zone 组件;禁直接 import L2 primitive / 贴裸 .ze-*);被 L5 组合拼装。总纲见 [DESIGN-SYSTEM.md](../../../../DESIGN-SYSTEM.md)。〔注:本文件旧标"L6"作废,唯一层号见总纲。〕

## 1. 这是什么

ZE 工作台的中区(Header / Footer 之间)是一个**可任意分割的容器**(layout split tree)。容器里每个叶子 slot 装一个 **editor** —— 把某种 entity_type 的数据渲染成可看可编辑形态的 UI 单元。

业务白话视角:

| ZE skill | 中区里业务同事看到的 editor |
|---|---|
| ZE-seek-find-object-generator | 场景工作台(`canvas2d` 渲染关卡 + 物体 bbox)|
| ZE-knowledge-base | 文档正文区(`text` 渲染条目)|
| ZE-data-query-workbench | 任务列表(`task`)→ 查询结果(`table`)→ 材料详情(`review`)三屏切换 |
| ZE-keyframe-extractor | 视频时间线(`timeline`)+ 关键帧画布(`canvas2d`)同屏 |
| ZE-splat-scene-blaster | 3D 场景预览(`scene3d` 渲染 splat 资产)|
| ZE-skill-system | skill 注册表(`table`)+ 选中 skill 详情(`form`)|

**关键定位**:editor 是中区的**渲染单元**,不是中区整体的"主形态"。一个 skill 中区可以装 1 个 editor、也可以分割成多个 editor 同屏。

## 2. 边界与接口契约

### 2.1 跟整张工作台的关系(4 级组合链)

```
UI 模块 (.ze-button / .ze-input ...)      ← L2  03-css-rules/02-atoms
   ↓ 组成
功能区 zone (ManageZone / ToolZone / 内容功能区)    ← L3  03-css-rules/03-zones
   ↓ 拼装
editor (本叶子: canvas2d / property / cardlist / ...)     ← L4  本文件
   ↓ 组成
skill (业务方的 ZE 工作台实例)             ← mod / 02-mod/04-layout.md
```

**编辑器自包含**:一个 editor 内部只由**自己的**功能区拼成 —— 身份横栏(header,可选)+ 管理/工具区 + 内容区(列表/网格/画布/预览/表单/文本/日志…)[+ 自己的 statusbar]。zone 的位置规则归 [L3](../../../../03-css-rules/03-zones/SPEC.md);editor 只声明用哪些 zone,不重复定义 zone。

**editor 不是功能区拆分单位**:一个 editor 可以包含多个相关功能区,只要它们围绕同一批实体、同一选择态、同一命令闭环工作。检测摘要、搜索排序、问题日志、实体列表、导入导出、批量命令通常属于同一个管理型 editor,不应拆成多个 editor。拆 editor 只在工作面/选择域/viewport/对照需求真正独立时发生;默认工作台 1-3 个 editor,超过 4 个必须说明每个都是独立工作面。

**编辑器结构 = 横栏(chrome)+ 工作区(语义边界)**(框架级统一,详见 [03-zones SPEC §6.3](../../../../03-css-rules/03-zones/SPEC.md);门禁 `EDITOR_BAR_NOT_BLOCK` + `BOUNDARY_GRAMMAR` 双向守):`.ze-editor` 玻璃壳里,顶部横栏(`__header`,身份/标题)+ 底部横栏(`__statusbar`,状态)是**编辑器自己的 chrome —— 贯通编辑器宽、单边发丝线、不成块**(横栏就是横栏);横栏之间是 `.ze-editor__work` 工作区(`padding`+`gap`),内含按语义选择边界的 zone:操作行(`toolbar`/`zone`)默认是 control group 承载槽,由按钮/分段控件表达边界;主工作面(`content`/viewport/canvas)必须保留 sunken surface + edge + radius + overflow;信息控制单元(`unit`)是 raised unit。横栏里只放身份/状态,操作(工具按钮等)挪出成工具功能区。**横栏 ≠ 功能区**:门禁双向焊死(横栏禁成块 / 工作区禁无语义硬套同款块),`__header`/`__statusbar` 纯 L2 统一、不许个别编辑器自定义横栏。

**editor 不内嵌别的编辑器的职责**:特别是**属性面板不是某编辑器的内嵌区** —— 它是一个**独立的 `property` 编辑器**,在 L5 布局组合里与主编辑器并排,绑同一 entity_type 读"当前选中"。`form` 只是低级字段/记录表单实现或 legacy alias。详见 §2.1b。

### 2.1b 编辑器自包含 + L5 多编辑器组合(三铁律)

两层:**① 单个编辑器自包含**(操作区+内容区);**② 多个编辑器在 LayoutTree 组合成工作台**。布局即数据(LayoutTree 可序列化、可分屏/拖/存)。

**三铁律**(散文法曾在 1.25.0 被同日违反——图层面板内嵌进画布;故 L3 已机器化,见下):

- **L1 只与唯一真相源通信**:编辑器读写的全部状态 = document store(实体)+ interaction store
  (选择)+ command dispatch(变更)。编辑器之间**互不感知、互不 React**——跨编辑器联动唯一
  通道是全局 `selection[entity_type]`:主编辑器选中谁,绑同 type 的详情编辑器就显示谁。
- **L2 三角色模型**:**卡片列表编辑器(cardlist, legacy gallery)= 某实体类型的 flat card/list 选择器与操作器**,
  **大纲编辑器(outline)= 某实体类型的 tree/group 层级选择器与操作器**;其选中经全局
  store 驱动 property(属性编辑器);画布类(canvas2d)是空间投影,同样只读 store(活动图层 =
  `selection[<layer 桶>]`,由装配缝注入,画布不持有选择态)。不管卡片列表承担什么业务角色
  (候选审阅/资产库/flat 图层列表/规则清单),它都适配——因为它只做"选择与操作某 entity"。
- **L3 编辑器不得实例化另一个编辑器**:`*Editor.tsx` 禁互 import(含 primitives/ 下);组合
  **只在 L5 LayoutTree** 发生。mod custom renderer 引用 core 编辑器是显式逃生舱,不在此限。
  机器门禁:`editor_manifest_check.py` 的 `EDITOR_IMPORTS_EDITOR` + `PRIMITIVE_NAMED_EDITOR`。

标准内置编辑器按角色分三类:

| 类 | 编辑器 | 作用 |
|---|---|---|
| **主**(列多条、驱动选择) | table / cardlist / task / graph / timeline / chart / outline | master-detail 的"主",选中项驱动详情 |
| **详情**(绑选中 entity 或 entity range) | **property** / text / code / review / compare / media / asset / scene3d / html / conversation | 显示/编辑选中实体或产物;property 是属性编辑器 |
| **画布**(空间编辑) | canvas2d / scene3d | 空间投影和交互;配独立 property 编辑器当属性面板 |

**L5 常用组合**(同一套独立编辑器,换布局树拼出不同工作台):
- **画布-图层-属性**:canvas2d + **cardlist flat 图层列表或 outline 树状/分组图层** + property(属性),三个独立编辑器都绑 layer 实体,选择联动。← 图形编辑器(PS 图层工作台,`layered_canvas` 预设)
- **列表-属性**:table/cardlist(主) + property(属性)。**画布-属性**:canvas2d/scene3d + property。
- **参数-结果**:property + cardlist。**三栏目录**:list + 工作台 + property。**时间轴工作室**:cardlist + media + timeline + property。**列表-审阅**:cardlist/table + review。
- 预设见 `lib/presets.ts`(catalogBrowse / singleCanvas / pipelineBatch / linearProcess + 待补 layeredCanvas)。

### 2.2 default editor class

Core 提供一组 default editor class,**开集** —— mod 可注册自定义 editor class(见 §8)。公共文档优先使用标准名;旧名只作为兼容 alias:

| 标准名 | 兼容名 | 说明 |
|---|---|---|
| `cardlist` | `gallery` | 卡片列表/缩略图/候选池/flat 图层列表 |
| `property` | `form` | 属性编辑器;`form` 是低级字段表单实现 |
| `conversation` | `dialog` | AI 对话、人机问答、terminal profile 宿主 |

| editor class | 渲染形态 | 典型 entity 形态 |
|---|---|---|
| `chart` | 数据图表 / 量化 | 量化型:多实体数值聚合(bar/line/area/scatter/histogram/pie + KPI)|
| `canvas2d` | 2D 画布 | 空间型(spatial2d):坐标 / bbox / 形状 |
| `scene3d` | 3D 场景 | 空间型(spatial3d):mesh / splat / voxel |
| `timeline` | 时间线 | 时序型:帧序列 / 片段 / 关键帧 |
| `table` | 表格 | 列表型:同 type 多 entity 横纵展示 |
| `cardlist` | 卡片列表 | 列表型:同 type 多 entity 的卡片/缩略图/图层列表 |
| `property` | 属性编辑器 | 记录型或 entity range:选中实体信息、字段、动作、引用关系 |
| `text` | 文本编辑器 | 长文型:正文 / 段落 / 富文本 |
| `code` | 代码/SQL/JSON | 结构化文本:语法高亮 + linting |
| `media` | 视频/音频 viewer | 媒体型:纯播放预览,不带时间线编辑 |
| `graph` | 节点图 | 关系型:节点 + 边(pipeline / mermaid)|
| `pipeline` | 管线 notebook | 运行型:pipeline run 的 command cell、输入输出、日志、错误、artifact |
| `asset` | 资产 viewer | asset_ref 型:图片 / PDF / 资产包 |
| `review` | 审阅面板 | 流程型:可标注 / 评论 / approve 的对象 |
| `compare` | 前后对比 viewer | asset_ref 对比型:before / after,只读 |
| `task` | 任务运行台 | 长任务型:status / progress / cancel |
| `outline` | 大纲 | 层级型:parent/order/tree/group |
| `conversation` | 对话 | 消息流、提问、回答、agent/terminal profile |
| `html` | HTML | HTML source/preview/sandbox/render/edit |

兼容 alias `gallery`/`form`/`dialog` 仍在 `EDITOR_CLASSES` 中存在,但新布局和新文档不要继续写它们。

每个 editor class 在自己的实现文件里声明 `accepts_entity_shape: [...]` 标签集 —— mod 据此知道某个 entity_type 能塞进哪些 editor class。

### 2.3 Editor ↔ entity_type 关系

**N:N,由 mod 显式声明 binding,不由 core 写死**。

- 同一 entity_type 可被多个 editor 渲染(`object` 同时在 canvas2d 看分布 + table 看列表 + form 看详情)
- 同一 editor class 可装多个 entity_type(canvas2d 同时渲染 `level` + `object` 叠层)
- mod 在 [`02-mod/04-layout.md`](../../../../02-mod/04-layout.md) 通过 layout tree 声明每个 editor slot 装哪个 editor class、绑哪个/哪些 entity_type
- core 不提供 default binding —— mod 不声明就没 editor 显示

### 2.4 Editor 的输入输出契约

| 方向 | 接口 | 说明 |
|---|---|---|
| **读** | `editor.subscribe(entity_type, entity_id?)` | 订阅 entity 数据;Document/Entity 变化时自动 re-render |
| **写** | `editor.dispatch(command_id, payload)` | 所有写操作走 `01-command` 通道,editor 不直接 mutate entity |
| **资产** | `editor.assetUrl(asset_ref)` | 由宿主解析短期媒体票据 URL；缺票、过期或无权时返回空串，editor 不读 token/store、不自行拼 URL |
| **选区** | `editor.selection` ↔ Interaction State | editor 内 selection / focus / hover 同步到 [L7 Interaction](../../02-interaction/SPEC.md) 的全局 selection,跨 editor 联动 |
| **生命周期** | `mount / unmount / serialize / restore` | layout tree 增减 slot 时调用;serialize 出的 state 存进 LayoutTree,刷新可恢复 |

### 2.5 跟其他叶子的引用

| 本叶子引用 | 引用点 |
|---|---|
| [`01-core/01-data-structure/02-entity`](../../../01-data-structure/02-entity/SPEC.md) | editor 读 entity / 不直接写 |
| [`01-core/01-data-structure/04-field-types`](../../../01-data-structure/04-field-types/SPEC.md) | `form` / `code` editor 用 field schema 驱动渲染 |
| [`01-core/01-data-structure/03-asset`](../../../01-data-structure/03-asset/SPEC.md) | `asset` / `media` / `scene3d` editor 走 AssetStore |
| [`01-core/03-commands-operations/01-command`](../../../03-commands-operations/01-command/SPEC.md) | editor 内一切写操作通过 command dispatch |
| [`03-css-rules/03-zones`](../../../../03-css-rules/03-zones/SPEC.md) | editor 内部用 zone 拼装,zone 规则不在本叶子重复 |
| [L7 Interaction](../../02-interaction/SPEC.md) | selection / focus / hover / 进度 / 取消 走 Interaction 全局规则 |

### 2.6 不在本叶子范围

- **editor 内子模式(画笔 / 选区 / 移动 / 排序 / 过滤)**:归各 editor 实现,框架 SPEC 不约束
- **zone 的位置和默认可见性**:归 L3
- **中区 LayoutTree(EditorSlot 的容器 / 分割 / tab / 拖拽 / 拉伸 / 持久化)**:归 [01-layout 父级 SPEC](../SPEC.md)
- **editor 内 UI 组件的视觉(button / input 样式)**:归 L2

### 2.7 mod 重载点(§8 的预告)

mod 能做的事:
1. **声明 binding**:在 layout tree 里把 editor slot 绑到 entity_type / entity_id
2. **注册自定义 editor class**:`ze.editor.register(MyCustomEditor)`(开集)
3. **配置 default editor**:为某个 entity_type 指定首选 editor class,简化 layout 声明
4. **重载 editor 内 zone 组合**:在 layout 里覆盖某个 editor class 的默认 zone 拼装

完整接口签名见 §6(M1 接口)/ §8(mod 重载完整签名)。

### 2.7b 基础原始编辑器模块(可配置、可组合、可引用)

`canvas2d` 与 `pipeline` 是基础原始编辑器模块:

- `canvas2d` 提供统一 2D 工作区母体(视口、图层、像素层、bbox、绘制、导入/导出)。
- `pipeline` 提供统一 Pipeline Notebook 母体(command cell、输入/输出、日志、错误、运行控制、fan-out 矩阵)。

它们不是某个业务工具的专用面板。mod 扩展必须按以下顺序:

1. **配置**:通过 slot `view_state` 开关能力、映射字段、绑定 command、声明 ports/entity UI 配表。
2. **组合**:通过 L5 LayoutTree 把基础 editor 与 table/property/cardlist/task 等独立 editor 组合,靠 selection / status / dispatch 联动。
3. **引用**:若配置和组合都不够,用 `editor:'custom' + view_state.rendererKey` 注册 mod renderer,在 renderer 内 import `@ze/core/04-user-facing/01-layout/03-editor` 导出的 editor/primitives/model helpers 继续开发。renderer 必须继续使用 `CustomPaneProps` 的 `dispatch`、`assetUrl`、`statusOf`、`viewState`;不得复制 core 组件源码、不得手写 `.ze-*` CSS、不得直接 mutate entity。
4. **反哺**:同一扩展被 2-3 个工具复用时,回 build-v3 增加加性 view_state 字段 / port kind / primitive / editor class,并更新 SPEC、manifest、fixture、tests、MIGRATIONS。

这条规则把"基础能力可以二开"和"体系不能分叉"同时钉住:业务创新在 mod 生长,通用形态沉淀回 core。

**custom renderer 的选择读写(F8)**:`CustomPaneProps` 只带宿主类型的 `selectedId`/`onSelect(id)`;跨类型选择(读别的类型的 active 指针、写任意类型的选中)唯一入口是 `useEditorSelection()`(01-layout 导出,内部走公开 SlotContext,custom pane 内订阅安全)。

```tsx
import { useEditorSelection } from '@ze/core/04-user-facing/01-layout'

function MyPane(props: CustomPaneProps) {
  const sel = useEditorSelection()
  const rule = sel.selectedOf('rule')                    // ✓ 读:跨类型当前选中实体(无则 null)
  const findingId = sel.selectedIdOf('finding')          // ✓ 读:跨类型当前选中 id
  const pick = (id: string) => sel.select('finding', id) // ✓ 写:跨类型选中(null = 清空)
  // 宿主类型仍走 props.selectedId / props.onSelect(id),零迁移
}
```

错误姿势(两次真实回归,audit-v3):

```tsx
props.selection.rule              // ✗ CustomPaneProps 无 selection → undefined 读,首屏即崩
props.onSelectEntity('rule', id)  // ✗ 无 onSelectEntity → "is not a function",点击即崩
```

这两个字段曾存在于旧版 CustomPaneProps、后被删;mod 不得再依赖 props 上的跨类型选择通道,一律走 `useEditorSelection()`(或等价的 `useSlotContext().selection / .onSelect`)。

**跨面板高亮通道(F2,Rendering Hints 层)**:selection 之外的第二条跨面板联动通道——「A 面板选中某实体 → B 面板高亮一组相关对象」。状态住 `useInteractionStore.highlights: Record<entityType, string[]>`,**纯视觉会话态:不进 Document / OpLog / undo / 持久化,与 selection 互不干扰、两态可叠加**;经 SlotContext 可选字段 `highlights?/setHighlights?/clearHighlights?` 透传,custom pane 读写入口同样收束在 `useEditorSelection()`(`highlightedIdsOf(type)` / `setHighlights(type, ids)` / `clearHighlights(type)`,`[]` 等价清除)。

消费端:**视口类先行,列表类待 css 层配套**——canvas2d(spatial 模式)已消费:对高亮集内实体在 overlay 里画与选中态可区分的虚线描边(画布内绘制、消费既有 token,不新增 `.ze-*` class);gallery/table 等列表类编辑器的高亮视觉需要 css 层新增状态样式配套,本轮不做、留待 css 层 bump 时一并接入。

### 2.8 尺寸预设(响应式)

EditorSlot 可被用户在 LayoutTree 中任意拉伸([01-layout 父级 SPEC](../SPEC.md) §2.3),容器宽度变化跨度可能很大(同一画面里左侧 200px 窄列 + 右侧 1200px 主区都常见)。每个 editor class **必须在任意宽度下都可用、不溢出、不截断**——但达成方式分两族,不是所有 editor 都用离散断点。

**自适应方式两族(选其一,按内容性质决定,不可漏):**

| 族 | 谁属于 | 自适应机制 | 是否需要 `@container` 断点 |
|---|---|---|---|
| **reflow-type**(随宽度**改布局**) | 内容是列/网格/多列结构,窄了要降列/堆叠/隐次要列 | 容器查询切换离散预设 | **是,必须声明三档**(下表) |
| **fluid/fill-type**(随宽度**连续伸缩**) | 内容是单填充元素(canvas/video)、原生折行文本(textarea/正文)、或 `flex-wrap` 自换行的条带 | `width/height:100%` + 原生 wrap / `object-fit` / `flex-wrap` / 内部 JS(ResizeObserver/zoom) | 否(断点对连续伸缩无意义,标豁免) |

**reflow-type 的容器断点**(canonical px;**不可用 token**——`@container` 不吃 `var()`,断点常量集由 [css_layer_guard.py](../../../../scripts/css_layer_guard.py) 规则 6 钉死,合法集 `{320, 480, 768, 1024, 1440}` ±16 滞后):

| 档 | 容器宽(`@container`)| 预设要求 |
|---|---|---|
| `xs` | ≤320px | 极简:核心内容 + toolbar 图标 only;辅助 zone 全折叠;若不可用须显引导"拉宽以编辑" |
| `sm` | 320..480px | 紧凑:单列内容 + toolbar 图标 only;次要 zone 折叠 |
| `md` | 480..768px | 标准:单列内容 + toolbar 图标+文本;主 zone 显 |
| `lg` | 768..1024px | 宽:双列内容 / 大画布;辅助 panel 默认显 |
| `xl` | ≥1024px | 完整:多 zone 同显;最佳工作宽 |

实现约束:

- 用容器查询(`@container` / ResizeObserver)切换预设,**不**用 viewport media query(因为 LayoutTree 让 slot 可比窗口窄得多);根 `.ze-editor` 已设 `container-type:inline-size`([editor.css](../../../../03-css-rules/04-editors/editor.css))。
- 同一 entity 的视图在不同断点切换要无数据丢失(切换是 view 重排,不是 state 重建)。
- 切换不抖屏:断点临界给 16px 的 hysteresis(滞后),避免狂闪(故合法断点集含 ±16 变体)。

里程碑分档:reflow-type 在 M1 必须支持 `xs` / `md` / `xl` 三档;M2 补 `sm` / `lg`;M3 允许 mod 自定义断点。

**逐编辑器类型判定**(机制 + 是否需断点;新增 editor 必须在此登记一行):

| editor class | 族 | 自适应机制 | 断点状态 |
|---|---|---|---|
| table | reflow | xs 退化单列(隐非首列)· md 收窄 cell · xl 放宽 | ✅ 已实装([table.css](../../../../03-css-rules/04-editors/table.css)) |
| form | reflow | xs label/控件上下堆叠 | ✅ 已实装([form.css](../../../../03-css-rules/04-editors/form.css)) |
| gallery | reflow | `@container` 自适应列数网格;对比 md 以下转单列 | ✅ 已实装([gallery.css](../../../../03-css-rules/04-editors/gallery.css)) |
| timeline | reflow | 轨/标尺密度随宽切换 | ✅ 已实装([timeline.css](../../../../03-css-rules/04-editors/timeline.css)) |
| text | fluid | `textarea width:100%` + 原生折行 | 豁免(连续伸缩) |
| code | fluid | 等宽 + `white-space:pre` 横向滚动(不折不重排) | 豁免(滚动型) |
| review | fluid | 单列纵向堆叠,note `flex:1` 填充 | 豁免(已单列) |
| chart | fluid | KPI strip `flex-wrap` 自换行 + SVG `width/height:100%` 自缩放 | 豁免(内部缩放) |
| media | fluid | video `object-fit:contain` 填充 + 控件条 `flex-wrap` | 豁免(填充型) |
| canvas2d / spatial | fill | `.ze-canvas`/`.ze-spatial` 100% + 内部 JS 平移/缩放 | 豁免(canvas 内部) |
| scene3d | fill | `.ze-scene3d-host` 100%,渲染器内部自适应 | 豁免(渲染器内部) |
| asset | fill | 复用 spatial 基元 + 简单列 | 豁免(canvas 内部) |
| graph | fill | `.ze-graph__canvas` 100% + overflow:auto 平移 | 豁免(canvas 内部) |
| pipeline | reflow | toolbar wrap + fan-out matrix 横向滚动 + cells 单列 | ✅ 已实装([pipeline.css](../../../../03-css-rules/04-editors/pipeline.css)) |

> 豁免 ≠ 不管尺寸:fluid/fill 类型仍受 §2.10 容器适应契约约束(`min-0` + 滚动归属 + ellipsis),只是不需要**离散断点**。把"没做断点"从"漏项"转成"有意识、按内容性质判定的边界"。

### 2.9 加载/处理态的尺寸稳定契约(强制)

每个 entity 单元(表行 / 卡片 / 缩略图 / 画布 / 图表 / 3D / 媒体)在 加载(pending)/ 运算(running)/ AI 处理(busy)/ 错误(error)各态,**必须与加载后内容占据同一 footprint** —— 一张图、一张卡,加载前后本身一样大,只是显示效果不同;**禁止「先小后大」造成布局抖动(reflow)**。

实现:用框架级 primitive **`EntityLoadingFrame`**(`03-editor/lib/EntityLoadingFrame.tsx`)统一承载:

- 外层 wrapper 先把盒子尺寸定死(图/卡 `aspect-ratio`、表行固定 `height`、表单 `min-height`),内层据 phase 切 骨架 / 内容 / 错误,四态共用同一盒子 → 零 reflow。
- **pending + `placeholder:true`** → 骨架填满 footprint；pending 但无显式 marker 的既有实体仍保留内容，前端禁止从空值/文案/id 猜占位身份。**running / busy** → **保留内容、同尺寸**叠处理提示 + 进度环(`.ze-entity-busy`,不塌缩、不替换成小 spinner,且 `pointer-events:none` 不硬锁)。**error** → 保留实体，并在同一实体附近附着 compact ErrorPlaceholder；**done** → 真实内容。四态盒子不变。
- 视觉语言统一:一条 shimmer(`@keyframes ze-skeleton-shimmer`)+ 一个进度环(`ze-spin`),全 token 驱动、全在皮肤(`02-atoms/{feedback,placeholder}.css`),明暗两主题一致。

既有视觉编辑器已各自满足尺寸稳定(gallery 卡 `.ze-thumb__media{aspect-ratio:1}`、canvas 系 `.ze-editor__canvas-overlay{inset:0}`);`EntityLoadingFrame` 是新编辑器(如 `chart`)与 mod 的**唯一规范工具**,把该契约从约定变成可复用 primitive,避免各编辑器重造、尺寸不一。

## 3. 各编辑器规格(batch A 已采用标准;机器可读真源 = `03-css-rules/EDITOR_MANIFEST.json`)

> 解决"没讲清楚有哪些功能区、怎么分布、拉伸怎么办"。每个 adopted 编辑器在此声明:**功能区(zone)+ 分布 + 内置交互 + 断点行为 + 行模型**。manifest 是机器可读副本(被 `editor_manifest_check.py` 校验),本节是人读规格。**撤销/重做 UI 不在编辑器里出按钮**——它是工作台级全局(`webapp App` 的 ⌘Z/⌘⇧Z/⌘Y + footer 按钮);编辑器功能区禁渲 undo/redo 按钮(门禁 `editor_zone_no_undo_button` 守)。**例外**:画布类编辑器可有**画布聚焦作用域的键盘逐笔 ⌘Z/⌘⇧Z**(绘图工具惯例,瞬时草稿级,见 canvas2d mask)——经 `useEditorShortcuts` 挂 document(keydown 冒泡早于挂 window 的全局 undo),本地草稿栈非空时 `stopPropagation` 拦下撤一笔、栈空则放行给全局撤整条已提交 Operation;但**仍不渲按钮**,提交结果是标准 Operation,走全局撤销链。

### 3.1 通用约定(所有编辑器)

- **外框统一**:每个编辑器 = `.ze-editor`(flex column)+ 可选 `.ze-editor__header`(身份/标题/只读计数)+ `.ze-editor__work`(zones + content)+ 可选 `.ze-editor__statusbar`。旧 `.ze-editor__toolbar` 只作迁移期兼容,新编辑器用 header + ManageZone/ToolZone/Content Zone。
- **横栏行高统一**:`.ze-editor__header` / `.ze-editor__statusbar` 是固定 1×`--ze-row-h` chrome 行;标题统一 `fs-sm + semibold`,计数/轻状态统一小字。操作按钮、搜索、select 不放进 header,应进入 ManageZone/ToolZone,并由 zone 内控件锁高。禁止单个编辑器用 padding、wrap 或自定义 toolbar/header selector 把横栏撑成另一套高度。
- **功能区不是加壳**:功能区靠职责、顺序、控件组、rows/card flow、选中态、滚动归属和密度形成组织。普通摘要/日志/列表 section 不默认套 `panel` 或 `.ze-editor__unit`;只有 property/pipeline 的信息控制单元才使用 unit,只有显式 `data-boundary="panel"` 的少数区域才画 panel。
- **行高系统**(见 [01-tokens SPEC §4](../../../../03-css-rules/01-tokens/SPEC.md)):语义同类行统一 `--ze-row-h`;控件 ≤ 行高居中、不撑行;多行控件 `data-multiline` = n×row-h。
- **键盘焦点**:编辑器 root `tabIndex=0`(承接局部快捷键,容器不显焦点环);局部快捷键经 `useEditorShortcuts`(作用域到聚焦的编辑器,不与全局 ⌘Z/⌘S 双绑)。
- **加载态尺寸稳定**(§2.9):pending/running/error 与 done 同 footprint,零 reflow。
- **写通道**:一切写走 `dispatch(command_id, args)`;编辑器不直接 mutate。

### 3.2 功能区可见性 / 可关闭约定

- 每个 zone 在 manifest 声明 `closeable: true|false`。**不可关闭**=编辑器闭环必需(header、主 content、关键 ManageZone/ToolZone);**可关闭**=辅助 zone(statusbar、secondary log、advanced details 等)。
- 可关闭 zone 的运行时开关 = `Interaction` 态 + `localStorage`(key `ze.zone.<skill>.<editor>.<zone>`),清缓存回 default;关闭按钮永远可恢复(不允许"关到找不回")。
- **现状**:batch A 编辑器(table/property/cardlist/canvas2d)的主 zone 均 `closeable:false`;table 的 `statusbar` 声明 `closeable:true` 但当前 demo 未渲染该 zone。**可关闭机制随"真渲染可选 zone 的编辑器"落地**,不预造无消费者的开关。

### 3.3 逐编辑器规格(batch A)

**table(列表型)** — header(标题/计数)· ManageZone(搜索/筛选/排序)· ToolZone(新增/批量命令)· body=`.ze-editor__tbl-scroll`(窗口滚动表)· statusbar(可关闭,运行/排队+取消)。
内置交互:左键单选 · ⌘ toggle · ⇧ range · **⌘A 全选 / ⌘⇧A 反选 / Esc 清空** · 点表头排序 · 搜索过滤 · 右键(改名/复制/删)· 行内删 · 实体级运行态。running/pending-real/error 都保留行、选择和右键；只有显式 placeholder pending 显骨架，error 紧邻原行附着；delete/adopt 在 UI 与 dispatch 两层禁用，普通命令可用。行模型:`th/td height=row-h`。
断点:xs 退化单列(隐非首列)· md 收窄 cell · xl 放宽 cell。框选:行铺满宽度,框选非自然手势,列表完整多选由 点击+修饰键+⌘A 覆盖(marquee 列 planned)。

**property(属性编辑器; legacy form 实现)** — header(标题 + 版本/生成进度/多选徽记)· ToolZone(可选业务动作条,声明了 actions 才渲染)· body=`.ze-editor__form-body`(schema 字段 + x-group 挂载期折叠组)· unit(可选:`.ze-editor__unit` 信息+控制单元,声明了 `sections` 时取代动作条+body；显式 `section.id` 作为个人折叠身份)。
内置交互:onBlur 提交 · enum→下拉 · format:color→取色器 · min/max→滑杆 · boolean→勾选 · array/object→JSON textarea(多行例外)· 分组折叠 · 动作派发(confirm 守卫/requiresFields 置灰/busy 单飞)。running/error 保留真实字段、参数、资产和预览；只有 danger/`entity.delete` 或显式 `conflictsWithRun:true` 的动作/控件在活动 run 中禁用，普通字段更新继续可用，冲突由后端 CAS 安全失败。行模型:单行字段 `min-height=row-h` 控件居中;`data-multiline` 字段 = 4×row-h。
断点:xs label 与控件上下堆叠(放开行高)。
**selection-follow(1.28.0,G-F 部分解冻)**:property 槽的 `entity_bindings` 是**允许类型闭集**(可多型);`view_state.followSelection: true` 时跟随跨类型「最近选中」(`lastSelected`,经 SlotContext 透传)。解析回退链在 `lib/inspectorTarget.resolveInspectorTarget`(纯函数):①follow 且 lastSelected ∈ bindings 且实体存活 ②primary 的 selection(**follow=false 与旧路径字节等价**)③其余绑定按序 ④null=空态。「槽内类型 tab(手动切型)」仍 defer(manifest plannedInteractions `in-slot-type-tabs`),等第 2 个真实消费者。
**动作区(ToolZone/actionbar 兼容层)**:`view_state.actions: FormAction[]`(单型)或 `actionsByType: Record<entityType, FormAction[]>`(多型,按当前类型取)。FormAction 闭集 = `{command, label(mod 数据直显), scope?: 'single'|'selection', confirm?, requiresFields?, variant?, conflictsWithRun?, result?: {kind:'download'}}`——P-4:无回调。`scope:'single'`(缺省)派发 `{type, id, expected_version}`(带 CAS 版本,内置 entity.delete/duplicate 可直接当动作;mod 命令忽略多余键);`scope:'selection'` 派发 `{type, ids}`(无多选退化 [activeId])。`conflictsWithRun` 是替换/删除目标的显式声明；缺省只有 danger/`entity.delete` 安全禁用，普通动作不被整条连坐。`result:{kind:'download'}` 是唯一首批结果消费：声明不得带 URL/path/file/MIME，也不能与自管 dispatch 的 `dialog` 并用；raw consumer 不进入 React Context、`SlotContext` 或 `CustomPaneProps`，而由 App→LayoutRenderer→内置 Form/Info 的受管 prop 单向传递，自定义 renderer 子树拿不到该 prop。App 只在同一 runtime namespace/generation 下，把 receipt 中同一 `document_id/operation_ids` 覆盖的 committed Operation `external.target_ref.file` 与唯一 succeeded ActionReceipt 合取，且要求 `action_id === op.command_id + ':' + op.id`，再请求固定 member-gated `/api/export/file/{name}`。consumer 缺失、失败/跳过/多 action/错绑定/切项目一律不下载，避免死按钮或串项目副作用。
**多选聚合编辑**:该类型 multiSelection>1 时 FormZone 进聚合模式(`entities` prop):同值字段直接编辑、异值字段「—」混合占位(checkbox 原生 indeterminate;混合数值滑杆退化数字输入),首次编辑覆盖全选区;**写回走内置 `entity.batch_update`(单 Operation,每实体 CAS,⌘Z 一键回滚)**;header count 槽显「已选 N」。聚合判定纯函数 `zones/aggregateFields`。共同字段只读聚合摘要(multiSummary)未做,defer。
**信息+控制单元(info-control-unit;1.35.0)**:`view_state.sections: InspectorSectionSpec[]`(单型)或 `sectionsByType: Record<entityType, InspectorSectionSpec[]>`(多型)声明时,property 渲为 **N 个 `InfoControlUnit` 的列表**(`zones/InfoControlUnit`)——每单元 = 信息区(`.ze-editor__unit-info[data-role=info]`)+ 控制区(`.ze-editor__unit-control[data-role=control]`)固定二分:**信息区不夹命令、控制收一处、嵌套限一层**(二分定义见 [03-zones/SPEC §2.6](../../../../03-css-rules/03-zones/SPEC.md))。`InspectorSectionSpec` 闭集 = `{id, titleKey, title?(直显), defaultOpen?, collapsible?, kind: 'fields'|'readonly-kv'|'params'|'controls'|'preview'|'variants'|'asset', fields?, paramSchema?, controls?, actions?: FormAction[]}`;`id` 必须非空且同一声明内唯一，既是 React identity，也是 `user×skill×project×slot + entity.type` 下的个人折叠 identity；不得从 title/titleKey/序号推导。重复 id 拒绝整份声明，避免两个单元共用偏好。`kind` 决定信息区(`fields`→逐 `FieldRow` 可编辑 / `readonly-kv`→只读 `InfoField` / `params`→`ParamPanel` / `controls`→声明式关键控件,支持嵌套 path 与可选 command 写回 / `preview`→asset_ref 预览 / `variants`→候选缩略图 / `asset`→asset_ref 上传挂载),`actions`(★控制区)复用 `FormAction` 闭集,与 ToolZone/actionbar 兼容层共享 `renderFormActionButton`(confirm/busy 单飞/requiresFields 置灰)。**声明 `sections` 与顶部 `actions` 互斥**(sections 优先,忽略顶部动作区);空 `sections` 走旧路径(动作区 + FormZone)**字节等价**(向后兼容)。`FieldRow`(`zones/FieldRow`)是字段渲染唯一真源,FormZone/InfoControlUnit 共享(纯受控,draft/jsonText/invalid 三态由调用方持有)。

**cardlist(卡片列表编辑器; legacy gallery 实现)** — header(标题/计数)· ManageZone(缩略图尺寸滑杆 + 可选搜索/筛选/排序/选择管理)· ToolZone(可选动作/导入/采纳/删除)· body=`.ze-editor__gallery-body`(`@container` 自适应列数网格)。
内置交互:左键单选 · ⌘/⇧ 多选 · **⌘A/⌘⇧A/Esc**(对**过滤后可见集**)· **marquee 框选** · 右键(复制链接/复制图/下载/采纳/删)· 拖拽重排(锁定卡禁拖)· 逐卡眼睛/锁定钮(`visibilityField/lockedField`,内置 entity.update CAS,点击不触发选卡)· 无缩略图一等公民(`showThumb:false` → 纯文本卡)· 选 2 张并排对比 · 实体级运行态。`cardSize: compact|balanced|spacious` 是 mod 默认 preset；搜索/筛选/排序/连续缩略图尺寸统一按 `userId/skillId/projectId/slotId` 写版本化 view-state slice，不进 Document/OpLog/undo。
断点:缩略图列数由容器宽度 + 连续滑杆计算出的 min-width 自适应;滑杆最大值自然单列且不横向溢出;对比 md 以下转单列。

**timeline(时序编辑器)** — header(标题/片段数)· ToolZone(时间缩放)· body=`.ze-timeline__lanes`(轨道 × 时间片段 + ruler + playhead)。
内置交互:片段选择 · 跨轨移动 · 左把手固定右缘 trim · 右把手 resize · 双击空轨按默认时长新增 · Del/Backspace 删除 · ruler scrub；全部写入只在 pointerup/命令边界派发，拖动草稿不写 Document。`view_state.maxTracks?:number` 同时是数据轨号、命中轨号和 lane DOM 的容量边界，缺省 64、框架硬上限 512；小数/负数/非有限/超大轨号必须经唯一规则变成容量内整数，不得静默隐藏实体。`view_state.dynamicTracks?:boolean` 与 `rangeCreate?:boolean` 都是**严格 opt-in、默认 false**：前者忽略固定 `tracks`，按数据最大占用轨渲染，未满容量时留一条末尾空轨、满 `maxTracks` 后不再生长；后者只在同时声明 `addCommand` 时允许空轨拖出范围新增。旧槽未声明两个开关时继续保持固定轨和双击新增语义，但不再允许无界 DOM。
拖范围是否有效只能比较**起点、终点分别 clamp + snap 后的值是否不同**；最小时长补齐只规范输出几何，不能把同一吸附点抖动、右边界点击或边界外拖动误判为创建。draft JSX 只拥有 left/width 几何与 `data-draft=true`，opacity/outline/pointer-events 等表现归受管 CSS。空数据默认显示标准空态；仅当存在 `addCommand` 且 `dynamicTracks`/`rangeCreate` 至少一个启用时，才展示第一条可操作空轨。

**canvas2d(统一 PS-lite 2D 画布母体,1.27.0 纯化)** — header(标题/轻状态)· ToolZone(可选 CanvasToolbar 工具/画布命令)· body=`.ze-editor__canvas-body`(视口;spatial/layered/像素层绘制共用同一工作区)。它是基础原始模块:mod 首选 `preset/features/tools/view_state command` 收窄;确需深度二开时在 custom renderer 中复用 `Canvas2dEditor` / `Canvas2dLayerStage` / `CanvasViewport` / 像素层与 spatial helpers,不复制画布。
**画布只做画布**:图层**列表与属性面板不在画布里**——图层=layer 实体(类型库 layer@1),flat 列表=cardlist 卡片列表实例(compact-list + 眼睛/锁/重排),树状/分组列表=outline 实例(parent/order + 眼睛/锁/重命名/复制/删除),属性=独立 property;经 L5 `layered_canvas` 预设组合,活动层=全局 `selection[layerType]`(三铁律 L2)。
内置交互:滚轮缩放到鼠标点(非被动监听,阻止页面滚动)· 中键平移 · 双击 fit · 拖入导图 · HUD 显缩放% + fit/加像素层/加 bbox 层/导出 PNG/复制 PNG;layered 模式:按 order 合成渲染 layer 实体 · 点层→全局选择 · 移动/工具建层(text/vector/导图/粘贴)· 笔刷绘制(画前预览、size/hardness 底缘 HUD 双滑杆、Shift+右键拖调、连续 stroke 插值;**绘制要求显式选中活动层**,空选只读)· 拖框裁切(锚尺寸+全层平移,逐层 CAS);可绘制像素层:画前笔刷预览、**画布键盘 ⌘Z/⌘⇧Z 逐笔撤销/重做**(聚焦作用域、瞬时草稿,工具区不出按钮——见 §3 撤销/重做口径)、clear 清空,输出尺寸严格等于 source natural size;工具区完整逻辑 = 工具(画笔/橡皮)→ 参数(大小/硬度)→ 编辑(清除)→ 运行(右对齐),组间 `.ze-editor__zone-sep` 细线分隔;spatial 模式:bbox 选择/变换 gizmo/框选(SpatialCanvas/useSpatialPointer)。

canvas2d 的核心数据模型是 `Canvas2dDocument` + `Canvas2dLayer`。层类型统一为 `base-image` / `raster` / `text` / `vector-shape` / `mask-selection` / `bbox-annotation` / `guide-output` / `group`;其中 `mask-selection` 是历史命名的像素选择层,不是独立业务模式。`role`/`meta.role` 可标记 annotation、reference、candidate、output 等业务角色,但 core 不解释它。大图和像素层都只保存 `asset_ref`,不把 base64 内嵌进 entity JSON。业务工具后续把某张像素层当 mask/range/alpha 消费,把某个 bbox/text 层当 prompt 标注消费,属于 mod 算法语义。业务工具通过 `Canvas2dPreset` 收窄能力,而不是复制画布:

| preset | 用途 | 默认能力 |
|---|---|---|
| `ps-lite` | 完整 2D 编辑器母体 | 图层、光栅绘制、橡皮、文字、基础形状、基础滤镜、transform 字段、像素选区、bbox、SAM/魔棒端口、导出 |
| `brush-selection` | finalizer 粗选 | 单 source + 可绘制像素选择层,brush/eraser |
| `single-range-layer` | Object Library 画范围 | 单 base-image + 单 range pixel layer,不改原 PNG |
| `bbox-placement` | seek-find bbox/placement | bbox + transform |
| `cutout-selection` | constraint selector / cleanup | 选区工具 + SAM/魔棒 + transparent PNG output |
| `readonly-preview` | 纯预览 | 只读视口 |

`view_state` 统一字段:`preset`、`bgField`、`layerType`(layered 数据源桶)、`layerAddCommand`(增层,缺省回退内置 `entity.add`)、`pixelLayerField`、`selectionLayerField`、`pixelLayerCommand`(正式像素层提交接缝)、`maskField`、`rangeMaskField`、`maskCommand`(legacy alias;仍指同一张像素层)、`geometryField` 或 `xField/yField/wField/hField/rotationField`、`tools`、`features`、`importCommand`、`paintCommand`、`bboxUpdateCommand`、`exportCommand`。编辑器只序列化操作 payload 和 asset ref;所有写操作必须 dispatch 到这些 command id,不得直接 mutate entity。layered 写路径(1.27.0 实体化):改层属性=内置 `entity.update` **逐层 CAS**;删层=cardlist/outline 的 `entity.delete` 或 mod `deleteCommand`;重排=cardlist 的 `reorderCommand` 或 outline 的 parent/order `entity.batch_update`;绘制提交=uploadAsset→`paintCommand` 回退 `entity.update {assetRef|maskRef}`。树状图层经 `parentId/order` DFS 拍平成画布栈,子层继承父层 hidden/locked。原 inline 栈面(`layersField/activeLayerField/layerUpdate|Delete|ReorderCommand`)已删(零消费者窗口,MIGRATIONS 1.27.0 记证据)。

**pipeline(管线链路主视图 + notebook 详情)** — header(固定管线标题/运行轻状态)· ToolZone(pause/interrupt/continue/cancel/retry + 导出 JSON/MD)· **chain(声明 `view_state.chain` 时的默认主链路:对象组 → command → 对象组)**· console(可选:StartConsole 操作台 cell)· summary(运行摘要 + interrupted 续跑提示)· matrix(fan-out item × stage)· cells(单 item notebook,可逐 cell 折叠)· details(`.ze-pipeline__details`:chain 模式下把 console/summary/matrix/cells 收进的展开详情容器)。
内置交互:pause · interrupt · continue · cancel · retry failed · run-from-step/rerun-step · 点击 fan-out 单元聚焦 step(自动展开目标 cell)· step 折叠(.ze-disclosure)· image/json 输出复制/下载 · notebook 导出(JSON/MD,纯前端只读序列化 `lib/pipelineNotebookExport`,asset 导 sha256 引用;无写副作用不需要命令)。数据源:`SlotContext.pipelineRuns` + `commandSpecs` + 可选 `pipelineSpecs`;cell 由 `PipelineRun.step_states` 自动生成。无 ports 的旧 stage 回退显示 status/logs/artifacts。phase 显示词一律过 `lib/taskStages.RUN_STATUS_ALIAS` 单收口点(→ `task.status.*` i18n);`data-phase` 属性保持原始 RunPhase 词(CSS 契约键)。
**链路主视图(view_state.chain;property-pipeline 对称重整)**:声明 `view_state.chain` 时,pipeline editor 默认渲**链路主视图**取代 notebook:`[对象组] → [command] → [对象组]`。对象组(`PipelineObjectGroup`)只组合既有展示语言(preview / `.ze-kv-list` / `ThumbnailGrid`),**不自画黑边卡片、不造专属 loading/error 皮肤**(状态来自 entity status,挂 `data-run-phase` 在自身 footprint 内进入态);command(`PipelineCommandUnit`)= `InfoControlUnit` 二分(相位/进度/错误=信息区、Run/Rerun=控制区),相位/进度/错误由 `lib/pipelineChain.phaseForMergedStages` 合并 `stageIds`(=`step_states` 的 **stage_id**,非 command_id)派生,与 entity 级 `phaseFromSteps` **同一真相源**(已抽到 `lib/pipelineChain`)。完整 stage notebook(StartConsole/summary/history/matrix/cells)降级为**展开详情**(`.ze-pipeline__details`,默认折叠,复用 showAll 状态)。闭集 `PipelineGroupSource`/`EntityDisplaySpec`/`ChainArgSpec` 见 `lib/pipelineChain`(非法回退旧视图;无 `chain` 的旧槽与 `display.cells` 字节等价)。**与 form 属性编辑器对称**:form 解释「一个 entity」(Info sections + Control actions),pipeline 解释「entity 如何被 command 转换」(对象组 + command 链),二者共用 `InfoControlUnit` 二分与 entity 状态模型。
**操作台(StartConsole,1.28.0,Jupyter 第一 cell)**:`view_state.startCommandId` 声明且命令在册时,`.ze-pipeline` 顶部渲参数 cell(`data-cell="console"`):start 命令 `params_schema` × ParamPanel 驱动表单;默认值三层 merge = schema default → `view_state.startArgs`(固定参数)→ `view_state.argsFromSelection`(`{argName: entityField}`,选中实体注入,纯函数 `lib/pipelineConsole.buildStartDraft`);Run 置灰 = required 缺值 ∨ in-flight ∨ 绑定 run 活跃(**固定管线一次一跑**)。**requires_runtime**:start 命令缺外部运行时 → 返回空 patches op + `op.error={code:'requires_runtime', message, missing[], hint}`,console 原地渲缺位块;其余硬错误由 footer 错误条承接(console 不复述)。
标准业务态下,每个工具的 pipeline editor 固定服务一个 pipeline,由 `view_state.pipelineId` 或 `view_state.startCommandId` 声明;run 选择只作为调试/历史浏览能力,必须显式 `allowRunSwitch:true` 才打开。entity 显示由 `view_state.entityUi` 配表声明(如 `labelField/thumbField/resultField/itemIdField/statusAnimation/summaryFields`),core 只按配表和 port kind 渲染,不写业务 if。
定位:footer 继续做全局摘要;pipeline editor 做某条 pipeline run 的 Jupyter 式复盘和操作。
**刻意设计(不是缺口,禁打补丁)**:① RunPhase **无 queued 相位**——pending 即排队,显示层经 RUN_STATUS_ALIAS 映射为「排队」,有显式测试钉死,禁在后端加相位;② `pipeline.start` **永属 mod**——framework 只内置 7 个 lifecycle 命令(pipeline_commands.py),启动含 WorkList/StagePlan 构造是业务语义,经 startCommandId 声明给操作台;③ 固定管线槽永远跟最新 run,历史浏览必须显式 allowRunSwitch(调试能力,非产品态);④ 不做 per-item pause / step 级 cancel——边界刻意收在 run 级 lifecycle + step 级重跑。
断点:视口自适应容器;工具条窄屏图标 only(规划)。

### 3.4 scene3d 成熟标准(来自 ZE 3D 系列)

ZE 系列里 3D 不是一个单一 viewer,而是一组共用 3D viewport 的不同业务:

| 来源 | 真实需求 |
|---|---|
| `ZE-image-to-3d-asset` | 单图/多视图生成 mesh、texture、preview、manifest |
| `ZE-image-to-scene-objects` | 单图点选物体,产出 per-object GLB,下游贴图/减面 |
| `ZE-image-to-splat-v3` | 高斯泼溅 `.ply/.splat` 候选审阅 |
| `ZE-model-render-studio` | 导入 GLB/glTF/OBJ,摆姿态、相机、截图并送 AI render |
| `ZE-model-retopology` | 原模型 vs 重拓扑候选,质量报告和线框对比 |
| `ZE-mesh-scene-blockout` | primitive scene blockout,选择对象、transform/material、导出 |
| `ZE-shader-node-workbench` | 节点材质图 + 3D 模型材质预览 |
| `ZE-push-pull-modeler` / `ZE-organic-sculpt-modeler` | 浏览器内几何编辑/雕刻,但输出仍走 command/manifest |

因此 core `scene3d` 的成熟边界是 **3D 工作区母体**,不是把 three.js 和所有 DCC 行为塞进 core:

- core 负责:EditorFrame、header/zones/content/statusbar 骨架、renderer host、尺寸稳定、running/error overlay、相机/显示模式的标准 view_state 信封、领域 `dispatch(command)` 接缝，以及 personal camera 的有界个人视图存储。
- mod 负责:three.js/splat/sculpt/NURBS/retopo 等重 renderer,通过 eager `renderer` 或 lazy `rendererModuleId` 注入。
- 大资产只走 `AssetRef`;GLB/glTF/OBJ/FBX/PLY/splat 不进 entity JSON。
- transform/material/领域 selection/领域 camera 等写入必须走 command；personal camera 是个人 Rendering Hint，只进 scoped view-state，不进 Document/Operation/undo。
- **会话多选是 Interaction State,不是领域态**:框选/加减选走 `multiSelection`/`onSelectMulti`(App 层 useInteractionStore)。**scene3d 通道 type-qualified 直通**(整表+带 entityType 的 SelectMultiFn),不像 outline/table 绑 slot primary——画布内实体类型经常≠slot 的 primary(实测:slot 绑 playable、视口选 scene_node,绑 primary 会把多选写错通道)。勿把会话选择写进 `selectionField`/`selectionCommand`——那对信封是领域态(持久化标记),每次点选变一条 Operation 会污染撤销栈,undo 还会回退选择。
- renderer props 必须是声明式信封,不得把裸 onClick/JSX 注入 core。

标准 `view_state`:

```ts
{
  meshField?: string
  rendererModuleId?: string
  cameraField?: string
  cameraMode?: 'personal' | 'domain' // 缺省 cameraCommand→domain,否则 personal
  selectionField?: string
  displayModeField?: string
  transformMode?: 'translate' | 'rotate' | 'scale'
  transformCommand?: string
  materialCommand?: string
  cameraCommand?: string
  selectionCommand?: string
  displayModeCommand?: string
  displayModes?: ('material' | 'solid' | 'wireframe' | 'normals' | 'uv' | 'splat' | {id:string,label?:string,labelKey?:string})[]
  controls?: { resetView?: boolean; wireframe?: boolean } // renderer 真实能力，缺省均 false
  gizmos?: ('translate' | 'rotate' | 'scale')[]
}
```

renderer props 标准信封:

```ts
{
  src?: string
  entity: EntityDict
  resetSignal?: number
  wireframe?: boolean
  displayMode?: string
  camera?: unknown
  cameraMode?: 'personal' | 'domain'
  cameraIdentity?: string
  selection?: unknown
  transformMode?: string
  onCameraChange?: (camera: unknown, meta?: {settled?: boolean}) => void
  onSelect?: (selection: unknown) => void
  onTransformCommit?: (patch: unknown) => void
  multiSelection?: MultiSelection            // 整表 Record<entityType, string[]>(type-qualified)
  onSelectMulti?: SelectMultiFn              // (entityType, id, mode, orderedIds) 直通,不绑 slot primary
}
```

成熟交互分层:

| 层 | 进 core | 留 mod renderer |
|---|---|---|
| viewport | personal/domain ownership、scoped 相机记忆、reset/home 与 wireframe 信封；外壳按钮必须经 `controls` 逐项 opt-in，默认不展示 | 具体 orbit/pan/zoom/线框实现、环境光、后处理；未在所有相关模式消费就不得声明 capability |
| selection | entity id / object id 信封、hover/selected data attrs | raycast 细节、mesh 子元素命中 |
| transform | translate/rotate/scale command 接缝 + gizmo 壳 | DCC 特有约束、push-pull、sculpt brush |
| inspection | bbox/mesh stats/material stats port renderer | retopo 质量算法、shader graph 编译 |
| export | artifact/manifest port | 三方 DCC 文件生成 |

下一步若 2-3 个 v3 3D skill 复用同一能力,按 EXTENSION-RULES P-6 反哺 `scene3d` 加性字段和 tests;单工具差异继续在 renderer 内。

### 3.5 卡片列表编辑器(cardlist)成熟契约(卡片视图 ≠ 表格列表)

**正名**:`cardlist` 是公共标准名,`gallery` 只作为 legacy alias。它的本质是**通用卡片容器编辑器**——某实体类型的选择器与操作器(三铁律 L2)。缩略图可有可无、眼睛/锁可有可无,初始密度由 mod preset 决定,用户可用明确的"缩略图尺寸"无级滑杆临时/项目级调节;每行数量由容器宽度和当前 min width 自动涌现,带 ManageZone 与 ToolZone。**PS 式图层列表就是它的一个实例**(compact-list + visibilityField/lockedField + reorderCommand,见 layered_canvas 预设)。

ZE 系列的卡片视图来源不是 table 的紧凑列表,而是"一个对象摘要 + 缩略/状态 + 快速动作"的主编辑器:

| 来源 | 卡片类型 |
|---|---|
| `ZE-seek-find-object-library` | object asset cards:透明 PNG、标签、质量风险、去重组、range mask 状态 |
| `ZE-knowledge-base` | knowledge cards:概念、一句话、主题、标签、学习状态、关联 |
| `ZE-reference-pack-builder` / image/video pipeline tools | candidate/result cards:缩略图、score、adopt/delete/compare |
| `ZE-video-export-conformer` / `ZE-video-retime` | preset/result cards:平台预设、规格摘要、输出状态 |
| `ZE-word-cloud-map` | concept candidate chips/cards:词、权重、保留/剔除状态 |

裁决:

- `table` = 行列数据、排序筛选、批量编辑,适合结构化字段密集比较。
- `cardlist` = card editor,适合对象摘要、候选审阅、资产库和知识卡。它可以呈现为网格或卡片列表,但仍不是 table。
- "合并表格那种列表"不算 card view;如果主信息是列字段和表头,用 `table`。

`cardlist` 的成熟 `view_state`(闭集;EDITOR_MANIFEST viewState 串是本表机器副本):

```ts
{
  layoutMode?: 'grid' | 'card-list' | 'compact-list'
  thumbField?: string
  labelField?: string
  descriptionField?: string
  scoreField?: string
  summaryFields?: string[]
  adoptCommand?: string
  deleteCommand?: string
  reorderCommand?: string
  allowAdd?: boolean               // 开「新增」入口 → 框架 onAddFor(entity.add + newEntityDefaults),与 table/document 同形;
                                   // 缺省关(不给已有卡片网格凭空长按钮)。没有它时该实体类型的唯一生产者只剩 mod 命令或 AI。
  emptyTextKey?: string            // 空态文案 i18n key(如 'mod.gallery.empty');缺省 'editor.empty'(只说没有,不说怎么才能有)
  // —— 卡片列表抽象拓展(1.27.0;全可选,缺省=原行为)——
  searchFields?: string[]          // 开搜索框 + 覆盖字段(entity.name 恒参与)
  filters?: GridFilter[]           // 状态 pill 组(values 缺省按字段去重派生)
  sorts?: CardSort[]               // 排序下拉(纯视图态,字段映射 + 比较类型 + 可选枚举顺序)
  defaultSort?: string              // 默认排序 id;缺省取 sorts[0]
  actions?: GalleryAction[]        // 动作区:{command,label,scope:'selection'|'global',confirm?,variant?};selection 动作常驻
  cardSize?: 'compact' | 'balanced' | 'spacious' // mod 默认 preset;内部映射初始连续 scale,可被本地滑杆偏好覆盖
  thumbSize?: 'sm' | 'md' | 'lg'   // deprecated/internal legacy;新代码禁写;运行时连续尺寸偏好不进 view_state
  visibilityField?: string         // 逐卡眼睛钮 → 内置 entity.update CAS
  lockedField?: string             // 逐卡锁定钮(锁定卡禁拖)
  showThumb?: boolean              // false = 无媒体区一等公民(纯文本卡)
}
```

`CardSort` 是纯声明式闭集:

```ts
{
  id: string
  labelKey?: string        // 推荐;也可 label 兼容语言中立显示
  label?: string
  field: string            // entity 顶层字段(name/id/version...)或 data 字段;支持 data.xxx
  valueType?: 'string' | 'number' | 'natural' | 'enum' | 'boolean'
  direction?: 'asc' | 'desc'
  order?: string[]         // enum 排序优先级;未列值排在已列值之后
}
```

搜索、筛选、排序和缩略图尺寸都属于 ManageZone,且只改变当前视图,不 dispatch、不写领域数据。
`cardlist` 的可见集计算顺序固定为 `filterCards → sortCards → GridZone`;
全选、反选、范围选择和框选的 `orderedIds` 必须跟排序后的可见集一致。

`outline` 的成熟 `view_state`(树状/分组图层、目录、块树共用):

```ts
{
  parentField?: string          // 缺省 parentId
  orderField?: string           // 缺省 order
  labelField?: string           // 行标题 data 字段;缺省 entity.name
  iconField?: string            // 图标名字段
  thumbField?: string           // asset_ref 缩略图字段
  badgeField?: string           // role/status 等 badge 字段
  visibilityField?: string      // 眼睛 toggle;false=hidden
  lockedField?: string          // 锁 toggle;true=locked
  renameField?: string          // 双击改名字段;name/__name 可写信封 name
  deleteCommand?: string        // 缺省 entity.delete
  duplicateCommand?: string     // mod 声明;无则不渲复制
  actions?: ToolbarAction[]     // 额外控制行命令
}
```

outline 是 tree/group 工作面,不是 canvas2d 的子面板。它只能读写实体字段和派发命令:
眼睛/锁/重命名走 `entity.update` CAS;删除走 `deleteCommand ?? entity.delete`;
复制走 mod 的 `duplicateCommand`;拖拽 reparent 走 `entity.batch_update` 批量写 parent/order。

三种形态:

| mode | 用途 | 行为 |
|---|---|---|
| `grid` | 候选图、素材库缩略浏览 | 多列缩略图、框选、多选、对比、拖拽重排 |
| `card-list` | 知识卡、资产审阅、预设选择 | 单列富卡,左缩略/图标,中摘要,右动作 |
| `compact-list` | 右侧窄栏、运行结果、短候选 | 单行卡,保留缩略/标题/动作,隐藏长摘要 |

所有模式共用 `ThumbnailGrid/ThumbnailCard` primitive,通过 `data-layout` 改排布;视觉在 L2 `thumb.css`,L4 `gallery.css` 只保留 legacy 结构类。`adopt/delete/reorder` 都是 command seam,其中 `deleteCommand` 缺省回退 `entity.delete`。新增卡片字段先通过 `summaryFields` / `descriptionField` 配表解决;同类业务复用后再增加闭集字段,不得在 mod 里复制 `.ze-thumb*` CSS。

### 4. 其余编辑器

manifest 中 `status: "adopted"` 的 editor 都必须有功能区、交互、fixture 和规格来源。新增 editor class 不从"好看的面板"开始,而从:实体形态、view_state 配表、command 写路径、CSS 分层、fixture、门禁一起开始。

## 6. M1 接口 (已实装)

M1 落地 12 种 editor class 里的 **`table` + `form`** 两种;新公共口径对应 **`table` + `property`** (覆盖列表型 + 记录型, 够撑起
"列出 entity → 选中 → 改字段" 的最小中区闭环)。组件是**受控件** (props 进 / dispatch 出),
SPEC §2.4 的 `editor.subscribe` 由上层 provider (webapp `useDocument` 订 SSE 后重喂 props) 实现。

```ts
// 写通道 (SPEC §2.4): fire-and-forget; 父层据响应 / SSE 重喂 entities props 触发重渲染
type DispatchFn = (commandId: string, args: Record<string, unknown>) => void

// EntityDict = Entity.to_dict() 的前端形态 (id/type/name/version/deleted/read_only/data)

// table: 列出某 type 的多 entity
TableEditor(props: {
  entityType: string
  entities: EntityDict[]
  columns?: { field: string; label?: string }[]   // 不传则从 data keys 推导
  selectedId?: string | null
  onSelect?: (id: string) => void
  onAdd?: () => void              // 新建带什么默认字段是业务知识, 交给 mod
  dispatch?: DispatchFn           // 不传用 EditorDispatchContext
})  // 行内删除 → entity.delete (软删)

// form: 编辑 1 个 entity 的 data 字段
FormEditor(props: {
  entity: EntityDict
  schema?: EntityTypeSchema       // 有则按 properties 渲染字段; 无则按 data keys
  dispatch?: DispatchFn
})  // onBlur / checkbox change → entity.update {type,id,patches:{field:value},expected_version}

// dispatch 注入
<EditorDispatchContext.Provider value={dispatch}> ... </EditorDispatchContext.Provider>

// 开集注册表 (12 class; M1 仅 table/form 非 null)
EDITOR_CLASSES: Record<EditorClass, ComponentType | null>
```

- 写操作一律走 command dispatch, editor 不直接 mutate entity (§2.4)。
- `entity.update` 的 `patches` 是 `{field: new_value}` 字典形态 (对齐 builtin entity.update)。
- 响应式 M1 三档 (`xs`/`md`/`xl`, §2.8): 容器查询 `@container` (非 viewport), 16px 滞后;
  见 `03-css-rules/04-editors/editor.css`。
- 文件: `index.tsx` / `TableEditor.tsx` / `FormEditor.tsx` / `types.ts` / `lib/dispatch.ts`。

## 7. Selection ↔ Interaction State 联动

Editor 是受控件,**不拥有 selection 真相** —— 它只发"选择意图",由宿主写进 Interaction State(会话态、不可 undo;State Layers 见 [02-interaction SPEC](../../02-interaction/SPEC.md))。

- **发选意图**:单选 `onSelect(id)`;多选 `onSelectMulti(id, mode, orderedIds)`(传了则优先,加性,不传退回单选)。
- **SelectionModel**(`lib/selectionModel.ts`,纯函数 + 不可变):
  ```ts
  type SelectionMode = 'replace' | 'toggle' | 'range' | 'all' | 'none' | 'invert' | 'set' | 'set-add'
  selectionModeFromEvent(e): SelectionMode        // ⌘/Ctrl→toggle、⇧→range、否则 replace
  applySelect(prev: string[], id, mode, orderedIds, anchor): { next: string[]; anchor: string | null }
  ```
  `orderedIds` = 当前可见顺序(range 据此算区间;all/invert 据此算全集)。`anchor` 是 range 的锚点,reducer 自己维护。
- **键盘**(`useEditorShortcuts` 是**通用 hook**,键位由调用方传入 ShortcutSpec;**焦点作用域** —— 只在 editor 获焦时生效,不抢全局):各编辑器绑 ⌘A=all / ⌘⇧A=invert / Esc=none。框选(`useMarquee`,gallery 演示)按 `additive` 由调用方映射为 `set`/`set-add`。
- **选择驱动内容**(editor-architecture 铁律 6):property 面板内容由当前 selection 派生,响应式而非命令式。
- **跨类型最近选中(1.28.0)**:Interaction Store 记 `lastSelected: {entityType, id} | null`(onSelect 非空即更新/onSelectMulti 跟随;清空匹配类型归 null),经 SlotContext 透传;property 槽 `followSelection: true` 据此跨类型跟随(解析在 `lib/inspectorTarget`,见 §3.3 property)。
- **联动边界**:**单 editor 内**的多选 + 键盘 + 框选 = 已 wired(9/9)。**跨 editor** 同类型选择同步天然共享 `selection[type]`;跨类型跟随由 lastSelected 承接(上一条)。
- **与 undo 的关系**:selection 是 Interaction State,**不可 undo**;数据写才进 Operation。undo/redo 是[工作台级全局](../02-footer/SPEC.md)(App ⌘Z/⌘⇧Z + Footer),editor **不**自绑,避免双重栈。
- **共享时间游标(C0)**:`currentTime`(秒)是与 selection 同级的**第二条**跨编辑器联动轴(Interaction State,纯会话态、不进 Operation、不可 undo;定义见 [02-interaction SPEC](../../02-interaction/SPEC.md))。editor 经 SlotContext 可选字段 `currentTime?` 读、`setCurrentTime?` 写——任意编辑器写,绑同轴的其他编辑器据 `currentTime` 跟随(scrub → 预览/关键帧/transport 联动)。**双向同步防回环**靠 `lib/playhead.shouldSeek`(纯函数:仅当与媒体实际时间差超阈值才 seek,吸收 timeUpdate↔seek 同帧回灌)。首个消费者 = `media`(原生 `<video>`);后续 timeline / compare(video)/ keyframe-curve / time-stage 复用同轴。未注入 = 退回纯本地播放(seam 闭合)。

## 8. mod 重载接口(完整签名)

mod 扩展 editor 全靠注册 + 注入,**不改 core**:

- **开集注册表**:`EDITOR_CLASSES: Record<EditorClass, ComponentType | null>`(`index.tsx`)。`EditorClass` 是 union;mod 注册自定义 class = 加一条目(null = 占位未实装)。机器可读真相 = [`EDITOR_MANIFEST.json`](../../../../03-css-rules/EDITOR_MANIFEST.json) + §3,不在此重复每个 props。
- **通用 props 形状**(受控):数据进(`entity` / `entities`)、`dispatch` 出;`statusOf(id)` 喂 entity 级运行态;`assetUrl(ref)` 由宿主用批量短期票据解析资产(editor 不直读 store、不接触 session token；缺票返回空串);多选走 `multiSelection` + `onSelectMulti`(§7)。
- **重渲染染料注入**(core 不打包重引擎):scene3d 经 `renderer?: Scene3dRenderer`(eager)、`view_state.rendererKey` 或 `rendererModuleId?: string`(懒加载,`useLazyModule` 托管 loading/error/retry)注入;`Scene3dRenderer` 接收标准信封 `{src?, entity, entityType?, entities?, selectedId?, entitiesByType?, viewState?, assetUrl?, dispatch?, onSelectEntity?, statusOf?, resetSignal?, wireframe?, displayMode?, camera?, cameraMode?, cameraIdentity?, selection?, transformMode?, onCameraChange(camera,{settled?})?, onSelect?, onTransformCommit?}`。`view_state.controls={resetView?,wireframe?}` 只决定外壳是否展示对应按钮，严格默认 false，不能从“renderer prop 是可选”猜支持；`cameraIdentity` 变化时 renderer 必须应用新 personal snapshot，或无 snapshot时回 transient home。高频引擎只在 settled 边界回传，core 仍有 debounce 兜底。单资产预览只用 `{src,entity}`;整文档 playable/scene runtime 可读取 `entitiesByType` 但语义仍留在 mod renderer。
- **binding**(entity_type → editor class):M1 webapp 装配点硬编码;M2 mod 在 `02-mod/04-layout.md` 声明;M3 全 layout tree。
- **dispatch 注入**:`<EditorDispatchContext.Provider value={dispatch}>` 或显式 `dispatch` prop(后者优先)。

## 9. 契约门禁(editor 自洽红绿)

[`scripts/editor_manifest_check.py`](../../../../scripts/editor_manifest_check.py) 把"每个 editor 想清楚了"钉成红绿,校验 [`EDITOR_MANIFEST.json`](../../../../03-css-rules/EDITOR_MANIFEST.json):注册表全覆盖(非 null class 必声明)、功能区 selector 在 CSS 存在、`rowModel=row-h` 的行 selector 真用 `var(--ze-row-h)`、adopted 必有 fixture、`builtinInteractions` 非空、01-core 未被 gallery/fixtures 污染(隔离铁律),并且每个 adopted editor 必须有 `editors/<id>/` 子模块、代码入口、README/CONSUMPTION/VALIDATION。

- **现状**:内置 editor class 以 `EDITOR_CLASSES` + `EDITOR_MANIFEST.json` 为真源。
- 新增 editor = 加 manifest 条目 + fixture;CSS 里不许有未声明的 `.ze-editor--X`。

## 10. reference(editor → 组件 → 真源 → fixture)

props 完整真相 = `EDITOR_MANIFEST.json`(机器可读)+ §3(人读规格);本表只做导航,**不复制**签名(防漂移)。

| editor class | 组件文件 | fixture(交互 gallery) | 状态 |
|---|---|---|---|
| 所有内置 editor | `editors/<id>/<Name>Editor.tsx` | `EDITOR_MANIFEST.json` 的 `fixture` 字段 | 以 manifest 为准 |

## 11. 里程碑

- **M1** — ✅ 单 editor 中区、`table` + legacy `form` 两种 editor class 落地;公共消费正名为 `property` (受控件 + dispatch 写通道
  + xs/md/xl 容器响应式)。binding 由 webapp 装配点硬编码 (item→table/property); 见 §6。
- **batch A** — ✅ editor class adopted 以 `EDITOR_MANIFEST.json` 为准;legacy `form/gallery/dialog` 分别由 `property/cardlist/conversation` 正名承接。
- **M2** — ✅ 多 editor 同屏已落地:`LayoutRenderer` 递归渲染 split/tabs/slot + 可拖分隔(`setRatio`)+ tab 切换(`setActiveTab`)+ `usePersistedLayout`(localStorage 持久化)+ 可序列化树;运行时结构操作 `splitSlot`(分屏)/`closeNode`(关闭并收敛)算子已实现,但**真实工作台 pane 的 UI 入口(tab 右键/×)未接**,目前仅在交互 gallery 的 layout 展柜真跑验证(`webapp/src/gallery/LayoutShowcase.tsx`;与 [01-layout SPEC §2.3](../SPEC.md) / FUTURE_STATE §7 口径一致)。mod 在 `02-mod/04-layout.config.ts` 声明 binding + 预设。仍待:`moveTab`(tab 拖拽重排,需拖放语义)、split/close 接入真实 pane chrome、editor 内 zone `collapsible`/`closable` 的非瞬时(常驻 zone)开关。
- **M3** — mod 自定义 editor class 注册接口开放;**跨 editor** 的 selection ↔ 全局 Interaction store 联动(§7 边界)。
