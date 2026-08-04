# Layout — 工作台整体布局调度

> **5 层体系 · L5 组合 / Compositions** — 只引用 L4 编辑器(声明 editor_class + 分屏 ratio);编辑器经 `selection[entity_type]` 全局选择联动。总纲见 [DESIGN-SYSTEM.md](../../../DESIGN-SYSTEM.md)。〔注:本目录另含工作台外壳(Header/Footer/Overlay)与交互态 —— 那些不属设计体系 5 层。〕

## 1. 这是什么

ZE 工作台屏幕分 **4 区**:Header / Main / Footer / Overlay。本叶子定义这 4 区的层级、Main 区内部的分割数据结构(**LayoutTree**)、用户拖拽 / 拉伸 / 折叠 / 合并 slot 的运行时行为、整套 layout 状态的持久化。

业务白话视角:打开任何 ZE 工作台都是"顶栏 + 中区(可分屏拼 editor)+ 底栏 + 浮层"四件套。本叶子讲这 4 件套怎么排、用户能怎么调、配置存哪。Header / Footer / Editor 子叶子讲各自内部细节;本叶子讲它们的**协作机制**。

层级上,本叶子是 `01-layout/` 的**父级 SPEC**,管 3 个子叶子(header / footer / editor)的拼装。

## 2. 边界与接口契约

> 跨编辑器联动法(指针级):编辑器之间互不感知,联动唯一通道 = interaction store 的全局
> `selection[entity_type]` + document store + command dispatch;编辑器不得实例化另一个编辑器,
> 组合只在本叶子的 LayoutTree 发生 —— 三铁律全文与机器门禁见 03-editor/SPEC §2.1b。

### 2.1 4 区结构

```
+--------------------------------------------------------+
| Header              (固定 28px,见 01-header/SPEC.md)   |
+--------------------------------------------------------+
|                                                        |
|   Main              (中区 = LayoutTree;装 editor)      |
|                     (本叶子 §2.2 - §2.6)                |
|                                                        |
+--------------------------------------------------------+
| Footer              (24/240px,见 02-footer/SPEC.md)    |
+--------------------------------------------------------+

Overlay 层(z 轴在以上 3 区之上):
  dialog / toast / context-menu / floating-panel
  由 L7 Interaction 调度,不参与 layout 计算
```

Header 和 Footer 各自固定一行,**不**进 LayoutTree;只有 Main 区可分割。Overlay 是浮层,不占布局空间。

### 2.2 LayoutTree 数据结构

Main 区内部是一棵**二叉分割树**(binary split tree),节点类型:

```python
# 三种节点类型
LayoutNode = EditorSlot | TabContainer | Split

EditorSlot {
  editor_class: str,           # 12 种 default 之一 或 mod 注册
  nameKey?: str,               # 可译编辑器标题(i18n key),优先于 name
  name?: str,                  # 语言中立回退标题
  entity_bindings: list,       # 这个 slot 渲染哪些 entity。多数 editor 取 [0](primary);
                               # form 槽例外(1.28.0):bindings=允许类型闭集,配合
                               # view_state.followSelection 跨类型跟随(lib/inspectorTarget)
  view_state: dict,            # editor 局部状态(zoom / scroll / selection)
  zone_overrides: dict?,       # editor 内 zone 的可见性 / 尺寸 override
}

TabContainer {
  children: list[LayoutNode],  # 任意 LayoutNode(允许 tab 内再 split)
  active_index: int,
  nameKey?: str,               # 作为(嵌套)tab 子节点时的可译标题,优先于 name;缺省 tab 头回退 'Tabs'
  name?: str,                  # 语言中立回退标题
}

Split {
  direction: 'horizontal' | 'vertical',
  ratio: float,                # 0.0..1.0,左/上节点占比
  first: LayoutNode,
  second: LayoutNode,
  nameKey?: str,               # 作为 tab 子节点时的可译标题,优先于 name;缺省 tab 头回退 'Split'
  name?: str,                  # 语言中立回退标题
}
```

**Tab 头标题解析**(`lib/tabLabel.ts`,三种节点统一):`nameKey`(经 `t()` 译)→ `name`(语言中立)→ kind 兜底(slot=`editor_class·primary_binding`;split=`'Split'`;tabs=`'Tabs'`)。
tab 直接子节点是 split 时,mod 必须在 `02-mod/04-layout` 给该 split 声明 `nameKey`(或 `name`),否则 tab 头只会显示裸 `'Split'`。

约束:

- 整棵 LayoutTree 必须 JSON 可序列化(serialize / deserialize 无损)
- Split 永远是二叉;**三栏 = 两次 Split 嵌套**
- TabContainer 的 `children` 可以是任意 LayoutNode(允许 tab 内继续 split,实现 IDE 风格的"tab + 分屏"组合)
- 不允许循环引用

### 2.3 用户运行时操作

> 本表是**目标操作集**。真实工作台 `LayoutRenderer.tsx` 当前只接了 **resize + tab 切换**;其余算子/UI 触发状态见"实现"列(状态真相以代码为准,本表只描述方向)。

| 操作 | 触发 | 行为 | 实现 |
|---|---|---|---|
| **拖拽 / 拉伸** | 鼠标按住 split 边界拖动 | 改 `Split.ratio`,即时刷新 | ✅ 已接(`SplitView` → `setRatio`) |
| **切换 tab** | 点 tab header | 改 `TabContainer.active_index` | ✅ 已接(`TabsView` → `setActiveTab`) |
| **新分屏** | — | — | ✗ **已移除**(2026-06-13 用户裁决:运行时新增 editor 槽非需求——新格空绑定无内容可选、悬浮 chrome 与 editor 内容重叠;`splitSlot` 算子与 slot chrome 一并删除) |
| **关闭 slot** | — | — | ✗ **已移除**(同上裁决,`closeNode` 算子一并删除;槽位增删由 mod default layout 静态声明,不提供运行时入口) |
| **折叠到最小** | 拖分割条到边界 | 设 `Split.ratio` 为最小值(见 §2.4),显折叠按钮可恢复 | ◐ ratio clamp 已生效;"折叠按钮可恢复"的按钮 UI 未渲染 |
| **合并 slot** | 拖一个 tab header 到邻近 slot | 移动 EditorSlot 到目标 TabContainer;源 Split 退化 | ✗ 跨容器移动未实现(`moveTab` 只做容器内重排,`TabsView` 已接) |
| **重置 layout** | Header → 设置 → 重置布局 | 回到 mod default layout(丢弃用户调整) | ✗ 入口未接(无 reset 接线) |

所有**已接**操作都即时反映到 LayoutTree(immutable update),保证 LayoutTree 是单一数据源。

### 2.4 尺寸约束

| 维度           | 最小值       | 来源                    |
| ------------ | --------- | --------------------- |
| EditorSlot 宽 | 240px     | 比 `--ze-space-60` 大一档 |
| EditorSlot 高 | 160px     | 容得下基本 toolbar + 1 行内容 |
| Header 高     | 28px(固定,对齐 v1)| L4 SPEC               |
| Footer 高(折叠) | 24px(固定) | L5 SPEC |
| Footer 高(展开) | 默认 240px,**用户可拖把手调整**,挤压中区(不浮) | 见 02-footer §2.5 |
【Footer 展开后，是折叠时候的条往上升，并且升起后可以自由拖动，不是固定的。升起后，是挤掉中区的区域，不是浮在中区上面，你理解下。所以中区的编辑器也会被顶的缩起来】
拖到最小后再拖 = 折叠到 0 + 显折叠按钮。

### 2.5 持久化

| 状态 | 存储位置 | 生命周期 |
|---|---|---|
| 当前 session | Interaction State 内存 | tab 关闭即丢 |
| 跨 session(同浏览器同 user) | `localStorage["ze.layout.<skill_id>.<doc_id>"]` | 清缓存即丢 |
| 跨 user / 跨设备 | **不同步** | mod 可在 `02-mod/04-layout.md` 声明 default layout 作为新用户起点 |

LayoutTree 是**单机状态**,不进 Document、不进 OpLog、不参与协作同步。

**恢复校验(core 1.33.0)**:`usePersistedLayout` 读到的持久化树,其**节点 id 集必须与当前 mod default 树完全一致**(用户能改的只有 ratio / active_index,不能增删节点),否则弃用持久化、回 default。防两类滞留:已移除的运行时增删功能曾写入的污染树(`n-*` 随机 id 空槽,删除入口已不存在);mod 升级换 default 布局后旧树残留。

### 2.6 tab vs split 选择指南

| 场景 | 推荐 |
|---|---|
| 不同 editor 需要**同时看**(canvas + timeline 对照) | split |
| 不同 editor 但**只用 1 个时刻看 1 个**(查询任务 → 查询结果 → 材料) | tab |
| 同 entity_type 多 id 对比 | split 或 tab,看屏幕宽 |
| 主 / 次视图(主画布 + 辅助列表) | 主 split,次默认折叠 |

mod 在 default layout 给推荐拼装;用户运行时可改。

### 2.6b Editor Slot 数量与拆分阈值

LayoutTree 组合的是**独立工作面**,不是普通功能区。默认工作台应保持 1-3 个 editor slot;超过 4 个 slot 必须在布局文档里说明每个 slot 的独立工作面理由,否则应合并到现有 editor 的 zones/body。

拆成多个 editor 的正当理由:

- 工作面交互模型不同,例如 canvas2d / cardlist / property / pipeline / media。
- 选择域不同,例如集合列表(master)与当前选中详情(detail)。
- viewport/滚动模型不同,例如硬 viewport 与内容流不能共享一个滚动者。
- 用户需要同时对照两个完整工作面。

不拆的内容:同一实体集合的摘要、筛选、排序、问题日志、检测结果、导入导出、批量命令和状态提示。它们是当前 editor 的功能区,用 zone、rows/card flow、summary strip、statusbar 或 afterZones 组织;不要因为看起来是"一块区域"就新增 editor slot。

### 2.7 跟子叶子的关系

```
01-layout (本叶子,父级)
  ├── 01-header   顶部固定栏(Header 内部)
  ├── 02-footer   底部固定栏(Footer 内部)
  └── 03-editor   EditorSlot 内 editor 渲染单元(中区一片叶子)
```

本叶子定义"4 区位置 + Main 区结构 + 运行时操作 + 持久化";子叶子定义自己区内的具体内容。Header / Footer 跟 Main 平级,**不**进 LayoutTree。

### 2.8 跟 mod 的接口

mod 在 [`02-mod/04-layout.md`](../../../02-mod/04-layout.md) 声明:

1. **default LayoutTree**:首次打开 doc 时长什么样(一棵默认 LayoutTree 的 JSON)
2. **default theme**(可选)
3. **Header / Footer 自定义**:转交子叶子接口(L4 §2.7 / L5 §2.6)
4. **EditorSlot binding**:default LayoutTree 里每个 EditorSlot 装哪个 editor_class、绑哪些 entity(L6 §2.3)

### 2.9 不在本叶子范围

- EditorSlot 内 editor 的实现 → [L6 03-editor](./03-editor/SPEC.md)
- Header / Footer 内部内容 → L4 / L5
- Overlay(dialog / toast / popover)的调度 → [L7 Interaction](../02-interaction/SPEC.md)
- 拖拽 / 折叠 / 切换的动效时长 → [L1 motion token](../../../03-css-rules/01-tokens/SPEC.md) + L7
- 分割条 / tab 视觉 → [L2 .ze-divider / .ze-tab](../../../03-css-rules/02-atoms/SPEC.md)
- LayoutTree 的多人合并 / 冲突 → 不支持;layout 永远单机

### 2.10 容器适应契约(flex 链 + 滚动归属)

中区从 `.ze-app` 到各 editor 内容是一条**纵向 flex 链**;新编辑器的容器接进这条链时,必须遵守以下契约,否则会出经典的 flexbox 溢出/截断 bug。契约由门禁 [`scripts/css_container_contract_check.py`](../../../scripts/css_container_contract_check.py) 静态守护(R1/R2 两条),CSS 实现住 [`03-css-rules/04-editors`](../../../03-css-rules/04-editors/)。

**C1 · flex 链收缩**:flex 链上每个容器必须声明 `min-width: 0`、纵向再加 `min-height: 0`。
> 为什么:flex 子项默认 `min-width/height: auto`(= 内容尺寸),**不会收缩到内容以下**;链上任一环漏了,内容就撑破父级而非让出空间。正例 `.ze-layout-root` / `.ze-split` / `.ze-split__pane` / `.ze-slot`([layout.css](../../../03-css-rules/04-editors/layout.css))均已 `flex:1; min-width:0; min-height:0`。

**C2 · 滚动单一归属**:整页不滚([app-shell.css](../../../03-css-rules/04-editors/app-shell.css) 在 `html,body` + `.ze-app*` 锁 `overflow:hidden`);滚动**只**发生在 editor 的 `__*-body` 容器,形如 `flex:1; min-height:0; overflow:auto`。同一 editor body 只有一个滚动者,内层子项 `overflow:hidden`。
> 门禁 R1 `CONTAINER_SCROLL_NEEDS_MIN`:任何 `flex:1 + overflow:auto|scroll` 的容器缺 `min-height:0` 即报。正例 `.ze-editor__task-body`([task.css](../../../03-css-rules/04-editors/task.css))。

**C2b · hug 不可压(滚动归属的对偶)**:滚动容器内的结构块(表单单元/信息卡/工具组行)是 hug 角色——`flex: 0 0 auto`,高随内容;需要省空间时滚动的是 owner,**不是压扁子块**。结构块自带 `overflow: hidden`(圆角裁切)时必须成对声明 `flex: 0 0 auto`(或显式 min-height):overflow 非 visible 会把 automatic minimum size 解析为 0,flex 直接把块压到近 0 高、数据不可读。已有内部 scroll-owner 的编辑器**禁开** `contentScroll`(双滚动条 + 淡出蒙版打错层)。
> 反例(本条来历):`.ze-editor__unit` 曾只有 `overflow:hidden` 无 flex 锁——form/property 单元在窄高视口被逐个压瘪,而 `__form-body` 明明是滚动 owner(batch-image-foundry 现场,下游先修 `2643d235e`)。落地:`__unit`/`__statusbar` + zone 内 `.ze-row`/`.ze-editor__filtergroup` 均已锁 `flex:0 0 auto`。

**C2c · 容器角色闭集**:链上每个容器归入且仅归入一种角色:`chrome-fixed`(横栏锁行高:`__header`/`__toolbar`/`__statusbar`)| `hug`(不可压:`__zone`/`__unit`/组行)| `fill`(分发层 `flex:1;min-height:0`、**永不加 overflow**:`__work`)| `scroll-owner`(`flex:1;min-height:0;overflow:auto`,一条链唯一:各 `__*-body`、`[data-content-scroll]` 的 content、`.ze-outliner`)| `viewport-clip`(pan/zoom 硬裁:`__canvas-body`)| `share`(比例瓜分+内部自滚:`__after[data-size="share"]`)。逐 class 义务表见 [editor-shell RULES](../../../03-css-rules/02-atoms/shell/editor-shell/RULES.md);8 结构类滚动角色见 [ZONE-CLASS-REGISTRY](../../../03-css-rules/03-zones/ZONE-CLASS-REGISTRY.md)。

**C2d · mod 自定义 zone/renderer 三条**:① 顶层要么 hug(用 `FunctionZone` 或自声明 `flex:0 0 auto`),要么显式接管 fill;② 要滚动就声明**恰好一个** scroll surface(三件套 `flex:1;min-height:0;overflow:auto`),其余结构块 hug;自身无滚动 owner 的整块内容交给 `contentScroll=true`;③ **收起=离场**:折叠面板一律 `display:none`/React 卸载,禁 width/height:0 的"视觉收起"——0×0 保活会让 `@container` 查询错档、懒加载 observer 死链;`display:none` 下两者惰性但自愈,重新显示即自动重算。

**C3 · 文本截断**:横向 flex 子项要单行省略号,必须 `min-width:0` 配 `overflow:hidden; text-overflow:ellipsis; white-space:nowrap` 一起出现。
> 为什么:同 C1——不给 `min-width:0`,flex 子项不收缩到内容宽以下,省略号永不触发。门禁 R2 `ELLIPSIS_NEEDS_MIN_WIDTH` 守护。正例 `.ze-task-row__name`、`.ze-editor__statusbar-op`([editor.css](../../../03-css-rules/04-editors/editor.css))。

**C4 · 悬浮 chrome**:行/卡级次要操作默认隐藏,`:hover`/`:focus-within` 才显(display 或 opacity 切换均可),不打扰内容、键盘可达。正例 `.ze-header__file-row-actions`(header-shell.css)。〔旧正例 `.ze-slot__chrome`(pane 分屏/关闭入口)已随该功能 2026-06-13 移除。〕

> 适应规则只到"容器如何接进 flex 链 + 谁负责滚动 + 如何截断";容器**内部元素的响应式重排**(随宽度换布局)是 editor 子叶子的责任,见 [03-editor/SPEC.md §2.8](./03-editor/SPEC.md)。

### 2.11 L5 组合响应式契约(工作台随容器宽自适应:手机/电脑同源)

§2.10 管单条 flex 链的收缩/滚动/截断;本节管**整个多 pane 工作台(L5 组合)随宽度自适应**——手机窄屏放不下 N 个 ≥240px 并排 pane(`MIN_SLOT_W`)是物理事实,必须退化。规则只到"L5 LayoutTree 如何按容器宽换排布 + 谁负责滚动";pane 内部 editor 的重排仍归各 editor(§2.8)。CSS 实现住 L2 [`02-atoms/layout-tree.css`](../../../03-css-rules/02-atoms/layout-tree.css)(split 是 L2 组件原子),按容器宽用 `@container` 表达。

**C5 · 工作区即响应式容器**:LayoutTree 中区根 `.ze-layout-root` 声明 `container-type: inline-size; container-name: ze-workspace`,L5 布局适配**按工作区容器宽**触发(非设备屏宽 → 嵌入/分屏也正确),与 editor 内部 `@container` 同范式。断点用 canonical 集 `{320,480,768,1024,1440}`(门禁 `BREAKPOINT_LITERAL`)。

**C6 · pane 布局归 CSS(消除 inline 例外)**:`LayoutRenderer` **不得**给 `.ze-split__pane` 写 inline `flex*`;split ratio 经 CSS 自定义属性 `--split-basis`(运行时值,非令牌)+ `data-pane="primary|secondary"` 传入,`flex` 由 layout-tree.css 拥有(`primary: 0 0 var(--split-basis,50%)` / `secondary: 1 1 0`)。
> 为什么:① 把 pane 布局所有权交回 CSS = 清掉历史「唯一例外」(TSX 写视觉/布局值);② 只有 flex 住 CSS,C7 的窄栏覆盖才能同特异性后置生效、**无需 `!important`**。`--split-basis` 经 inline `style` 传值是规则 8 `LAYER_INLINE_VISUAL` 明许的 CSS-var 传值(同 `--ze-avatar-url` 先例)。门禁:`responsive_check.py` R-RESP-3 守 SplitView 不回潮 inline flex。

**C7 · 窄栏纵向堆叠 + 整列滚**:`ze-workspace` 容器宽 `< 768`(canonical lg)时,所有 `.ze-split` 退化 `flex-direction: column`、两 pane `flex: 0 0 auto` + `min-height: var(--ze-stack-pane-h)`、`.ze-split__divider` 隐(窄栏无并排可拖);滚动归属由"editor `__*-body` 内部"上移到"`.ze-layout-root` 整列滚"——**本条在窄栏显式覆盖 C2 的「整页不滚」**,宽栏(≥768)行为零变化。每 pane 内 editor 仍按 C2 在自身 `min-height` 内滚(嵌套滚动,移动端标准)。
> 门禁 `responsive_check.py` R-RESP-2:`03-css-rules` 须存在含 `ze-workspace` + `max-width: 768px` 的 `@container` 块(防 C7 被静默删)。

**C8 · viewport meta 必备**:`webapp/index.html` 须含 `<meta name="viewport" content="width=device-width, initial-scale=1">`(缺则移动端整体不可用)。门禁 `responsive_check.py` R-RESP-1 守。

## 11. 里程碑

- **M1** — ✅ 4 区结构 + LayoutTree(EditorSlot + Split,二叉)+ 拖拽拉伸 + localStorage 持久化。**重置布局入口待接**(算子可由调用方重灌 default tree,UI 入口未接)。
- **M2** — TabContainer 渲染 + tab 切换 ✅;折叠 clamp ✅(恢复按钮未渲染);mod default LayoutTree 接入 ✅(`loadLayout`/`usePersistedLayout`)。〔运行时 `splitSlot`/`closeNode` 算子及 slot chrome 曾接入,2026-06-13 按用户裁决整体移除——见 §2.3 行为表。〕
- **M3** — `moveTab` 容器内重排 ✅(跨容器合并未做)+ floating-panel detachable(L3 zone 浮窗能力)+ 用户自定义 layout export/import + 跨用户分享 layout 模板

## §3-§10

> Stage B 填:§3 LayoutTree 序列化 schema、§4 unmount / restore 接口、§6 mod 注册 API、§9 audit gate(LayoutTree 不进 Document / 不进 OpLog)、§10 reference 表。
