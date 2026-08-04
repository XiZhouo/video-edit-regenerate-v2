# Functional Zone — 编辑器的功能分区

> **5 层体系 · L3 功能区 / Functional Zones** — 只引用 L2 原子,被 L4 editor 组合。功能区解决“这一块负责什么、怎么排、怎么收敛”,不解决业务语义。

> **结构类闭集权威 = [ZONE-CLASS-REGISTRY.md](./ZONE-CLASS-REGISTRY.md)。** 全系统只有 **8 个功能区结构类**(闭集,四产品实测收敛):状态区 / 控制条 / 列表 / 字段组 / 视口 / 树 / 输入区 / 决策确认。本文的 **管理 / 工具 / 内容** 是这 8 类的组织视角(管理+工具 = 控制条类的两子角色;内容 = 列表/字段组/视口/树/输入区)。CSS 只实现结构类、不认业务实例——selector 禁真业务域词,由 `zone_class_check` 门禁把关(DESIGN-SYSTEM §0.3 `ZONE_BUSINESS_SELECTOR`)。新 UI 只能引用已登记的 8 类;真出现落不进任何类的模式,回注册表加类,不私造。

## 1. 核心模型

一个 editor = editor chrome + 1..N 个功能区。

功能区是**一个相关工作闭环里的组织区域**,不是一层视觉壳。一个 editor 内可以有多个功能区:例如检测摘要、搜索排序、问题日志、实体列表、工具命令和状态条都可以同属一个"物体管理" editor。是否属于同一 editor,先看它们是否围绕同一批实体、同一选择态、同一命令闭环工作;不是看屏幕上能不能画出一块边框。

功能区只有三类公共职责:

| 类 | 标准名 | 职责 | 典型内容 |
|---|---|---|---|
| 管理 | `ManageZone` | 看什么、管哪些实体 | 搜索、筛选、排序、选择全集、全选、反选、清空、删除选中 |
| 工具 | `ToolZone` | 对当前对象做什么 | 画笔、橡皮、选择、transform、run、retry、pause、resume、command buttons |
| 内容 | `<EditorName>Zone` | 工作面本身 | Canvas2DZone、CardListZone、PropertyZone、CommandZone、OutlineZone、HtmlZone、ConversationZone |

旧名兼容:

| legacy | 新口径 | 说明 |
|---|---|---|
| `filterbar` | ManageZone | 旧筛选条组件可继续承接 ManageZone |
| `control` / `toolrow` / `actionbar` | ToolZone | 旧控制条/动作条可继续承接 ToolZone |
| `toolbar` | editor chrome 或旧工具槽 | 新操作按钮应进 ToolZone |
| `inspector` | legacy primitive | 公共产品名是 `property` editor,不是 inspector/form editor |

## 2. 责任边界

- ManageZone 只负责辅助查看和管理选择集合,不放编辑工具。
- ToolZone 只负责触发操作或调工具参数,不承载主数据。
- Content Zone 承载当前 editor 的主工作面,不塞别的 editor 的职责。
- 属性面板是独立 `property` editor,不是 canvas/table/cardlist 内嵌 aside。
- 图层 flat 列表是 `cardlist`,层级树是 `outline`,画布是 `canvas2d`,三者通过 selection 联动。
- 不把每个功能区拆成 editor。拆 editor 只在工作面/选择域/滚动与交互模型真正独立时发生;普通摘要、筛选、日志、检测结果、导入导出和批量命令都应留在当前 editor 的 zones/body 里。

## 3. 横条布局

ManageZone / ToolZone 都是横条,按内容选择布局:

| `data-layout` | 行为 | 用途 |
|---|---|---|
| `wrap-cluster` | hug 宽,自动换行,不拉伸 | pill 筛选、标签条件 |
| `flex-fill` | 自动换行,每行占满,子项按 `data-flex` 比例拉伸 | 控制按钮组、选择管理、命令组 |
| `hug-scroll` | 单行 hug 宽,空间不够横向滚动 | 图标工具条、画布工具 |

硬规则:

- 横条必须有明确滚动归属,不能把整个 editor 撑出不可控滚动。
- 子项必须有 min width;低于 min width 触发换行或滚动,不能挤压文字。
- 标准行高走 `--ze-row-h`;控件高不超过行高。
- `flex-fill` 子项可用 `data-flex="2"` 之类声明比例;同一行按比例分配剩余宽度。
- 换行后每一行仍按 row-h 节奏排列,不能靠 padding 撑高。

### 3.1 边界语法

功能区必须有边界,但边界不等于所有区域同款四边容器。边界由 core/L2 决定,mod 只声明 zone 类型、拓展点和 data 状态。

| 边界 | 用途 | 视觉原则 |
|---|---|---|
| `chrome` | `__header` / `__statusbar` | 单边发丝线或透明,贯通编辑器宽,不块化 |
| `control-group` | ManageZone / ToolZone 默认形态 | 外层不画四边块;按钮、分段控件、chip、组间分隔线表达边界 |
| `panel` | 少数需要独立面板感的操作区 | 显式 `data-boundary="panel"` 后才允许 raised + edge + radius |
| `viewport` | canvas / preview / image audit / media 主工作面 | 必须有 sunken surface + edge + radius + overflow,禁止裸奔 |
| `rows` | list / table / outline | 以行分隔、选中态、滚动边界表达结构,避免外层再套同款卡 |
| `unit` | property / pipeline 的信息控制单元 | raised unit,内部信息区和控制区二分 |
| `pass-through` | 内部已有完整边界的复合主体 | 只保留 flex/scroll 职责,不得再画一层视觉壳 |

硬规则:

- 默认 `.ze-editor__zone` 不画同款四边块;确需块边界必须显式 `data-boundary`。
- "有组织"不等于"加壳":优先用职责顺序、row/card flow、控件组、细分隔、选中态、滚动归属和密度表达层级。只有 `panel`/`viewport`/`unit` 这些语义成立时才画完整视觉块。
- `.ze-editor__content` 默认是 viewport 边界,用于保护画布/主工作面;只有内部首层已经有完整边界时才用 `contentMode="pass-through"`。
- `pass-through` 不是去边界许可,而是避免重复壳;子级必须自己提供 panel / rows / viewport / unit 边界。pass-through 父 content 即便可滚,也不能给整组子容器套 mask fade。
- L4 editor 和 mod 不写私有 border/radius/background 来补边界;只能消费 core/L2 提供的 class 与 data-*。
- 下游 `02-mod/05-renderers.ts(x)` 由 `scripts/mod_renderer_check.py` 把关:内联视觉、`.ze-editor__unit` / `.ze-editor__form-body` / editor chrome 类滥用会红;超过 4 个 editor 只给评审提示,用来防"每个功能区拆一个 editor"回潮。
- L4 editor 不手写 `.ze-editor__zone` 外壳;固定角色组件优先,无法归入现有角色时使用 core `FunctionZone` 兜底。
- L4/L5 CSS 不私有覆盖 `.ze-editor__content`;去掉主内容外框必须在结构层声明 `EditorFrame contentMode="pass-through"`。

边界判断顺序:

1. 先判断职责:这是管理集合、操作工具、主工作面、行列表、详情单元,还是内部已有完整边界的复合体。
2. 再选边界:工具/筛选默认 `control-group`;画布/预览默认 `viewport`;列表默认 `rows`;属性/管线信息块默认 `unit`;复合体才用 `pass-through`。
3. 再判断滚动:卡片/表单/日志/文本/对话流要 soft fade;canvas/media/graph/timeline 是硬 viewport,可以硬裁切;顶部状态/筛选 `wrap-cluster` 是功能区控件排布,不做纵向 fade。
4. 最后调密度:只调对应语义层的间距,不要因为某处拥挤就把所有区域都缩小。

反例口径:

- 不合规:一个普通 section 外面套 panel,里面只有一个 card/detail panel。
- 不合规:检测摘要、日志、列表各套一个 `.ze-editor__unit`,用 raised 块和段头栏把普通区域伪装成功能区。
- 不合规:因为有搜索、日志、列表、导出四块,就拆成四个 editor;这些通常是同一实体集合的管理功能区。
- 不合规:为了避免重复壳把 canvas/preview 直接裸放。
- 不合规:header/footer 当成页面装饰,和 editor shell 用不同 gutter/行高/边界。
- 不合规:custom renderer 里把选中实体详情按 schema 顺序全量摊开,再用 `details/summary` 或同款卡片壳硬收纳。
- 不合规:把多个 `.ze-field-row` 放进 `.ze-grid[data-cols="2"]` 半宽列里做属性矩阵;field-row 是整行字段原子,不是 grid cell 内部小控件。
- 合规:父级 pass-through,子级 rows/viewport/unit 提供自己的边界。
- 合规:同一个"物体管理" editor 内,顶部 summary strip + ManageZone 搜索排序 + rows 日志 + rows/card flow 物体列表共享一个清晰滚动归属。
- 合规:custom renderer 因业务需要把 master/detail 放在同槽时,列表自己是 rows/card flow 滚动归属,选中实体详情才是一个 `unit`,高级字段用 `.ze-disclosure` 分组且单列字段流。

### 3.2 Custom Renderer 详情区判定

custom renderer 仍按 zone 语法判定,不是私有页面:

1. 先问这块是不是「一组实体」还是「一个选中实体」。一组实体按 rows/cardlist 语言;一个选中实体按 property/unit 语言。
2. 当前选中实体详情默认只展示主任务闭环,例如摘要、一句修改、主要动作。技术字段、JSON、prompt、实现引用进入高级分组。
3. 高级分组必须是 disclosure 头 + 内容流。不要用原生 `details/summary`,不要给每个小分组再套 raised card。
4. 字段流默认单列;只有字段本身不是 `.ze-field-row` 且容器宽度足够时才考虑 `.ze-grid`。
5. 类型专属字段按类型显隐。空的 AI Prompt、空的代码路径、空的示例图不应在无关规则里占位。
6. 滚动只有一个明确归属。卡片/字段/文本流可以 soft fade;canvas/media/viewport/status/filter/control 不做 fade。

### 3.3 行>组>件(功能区容器三层)

功能区横条内的结构分三层,每层有自己的正解与禁形:

**件(单个控件)**

- 筛选值 pill = `.ze-chip`(`data-interactive` 未选 line-soft 轮廓 / `data-selected` accent 反相);元素保持 `<button>`,留住键盘可达与焦点环。
- 需轮廓的命令按钮 = `secondary`(静止可见轮廓);toggle = `secondary + data-active`;`primary` 一屏至多一个;`ghost` 仅低危高频辅助件。
- 反例:常驻命令用 `ghost` = 裸文字、可点性不可见(错);筛选 pill 用 `ze-button` ghost/primary 双态 = 未选无轮廓(错)。

**组(不可拆单元)**

- 短组(label+单控件,如「排序」+select)= `.ze-row`(nowrap),**不可压**:`flex: 0 0 auto`;长值集组(一组 chip)才允许内部 wrap。
- 组名恒完整:组内 `data-truncate` 文本锁 `min-width: max-content`,弹性归控件不归名;行内弹性由输入件承担(搜索框 `flex:0 1 auto; min-width` 兜底)。
- 反例:filtergroup 内部 wrap 把「排序」两字竖排(错);flex 压缩把组名咬字成「排…」(错)。

**行(横条整体)**

- 同类动作成组:声明式 `cluster` 优先(组序=首现顺序),命令名推断兜底;组间用分隔表达边界,不靠空隙。
- 溢出降级有序:筛选 wrap 超过 2 行 → 塌成「筛选(N)」secondary 按钮 + popover 面板(portal + 视口 clamp + Esc/外点关闭);图标工具条走 `hug-scroll` 横滚。
- 反例:flex wrap 里 shrink 优先于换行,可压的组先被压变形(按钮叠字、label 竖排)而不换行(错——组必须 `flex:0 0 auto`,editor-shell 已落地)。

### 3.4 按需渲染与命令行分级

功能件**无消费者则不渲染**(默认值站"用户只要有用的"一边,不站"展示框架能力"):

- 缩略图滑杆 ↔ 有缩略图(`showThumb===false` 时不渲染滑杆——纯文本卡列表的滑杆是死控件)。
- 选择簇(全选/反选/清空)↔ 存在 `scope==='selection'` 的动作;外门与内里共用同一判据,防"外门开着内里全空"的空 zone;⌘A 键盘多选能力保留。
- content 壳 ↔ 内容不自带边界;内容自带边框底色(卡片流等)必须 `contentMode="pass-through"`,不再套 sunken 壳链。
- 参考实现:`GalleryEditor`(orderedActionGroups 显式 cluster 优先 + 按需滑杆/选择簇 + pass-through)。

命令行分级:高频常驻(行内直显)/低频收纳(「更多」菜单 = 菜单原子消费)。静态可查项走门禁(`zone_layout_check`);"哪个命令算高频"是评审判断,列入评审清单、不假装门禁。

## 4. InfoControlUnit

`property` 和 `pipeline` 共用“信息 + 控制”二分:

| 区 | selector | 职责 |
|---|---|---|
| 信息区 | `.ze-editor__unit-info[data-role="info"]` | 字段、只读键值、参数、状态、引用关系 |
| 控制区 | `.ze-editor__unit-control[data-role="control"]` | 本单元 command buttons |

铁律:

- 信息区不夹命令。
- 控制收一处。
- 嵌套限一层。
- 所有修改走 command;批量修改是一条 Operation。

## 5. 编辑器组合表

| editor | ManageZone | ToolZone | Content Zone |
|---|---|---|---|
| `canvas2d` | 可选 | brush/eraser/select/transform/run | Canvas2DZone |
| `scene3d` | 可选 | camera/display/gizmo/export | Model3DZone |
| `cardlist` | search/filter/sort/selection | adopt/delete/import/batch actions | CardListZone |
| `property` | target scope/filter 可选 | actions/actionsByType/section actions | PropertyZone |
| `pipeline` | run/history filter 可选 | pause/interrupt/resume/cancel/retry/rerun | CommandZone |
| `outline` | search/filter/selection | rename/delete/reparent/batch actions | OutlineZone |
| `conversation` | role/thread filter 可选 | send/answer/retry/stop/terminal profile controls | ConversationZone |
| `html` | preview/source filter 可选 | refresh/render/open/export | HtmlZone |

## 6. 实现现状

- `FilterBarZone` 是 ManageZone-compatible 实现,仍保留旧 `filterbar` data-role。
- `ControlZone` 是 ToolZone-compatible 实现,仍保留旧 `control` data-role。
- `FormEditor` 承接 `property` alias,但 `form` 不再是公共属性面板名。
- `GalleryEditor` 承接 `cardlist` alias,但新消费写 `cardlist`。
- `DialogEditor` 承接 `conversation` alias,terminal 归 conversation profile,不作为独立 editor 推进。

## 7. 门禁规则

新增或修改 zone 时必须同步:

1. `01-core/.../03-editor/zones/types.ts`
2. 对应 zone 组件 data attrs
3. `03-css-rules/EDITOR_MANIFEST.json`
4. editor README / SPEC / fixture
5. `scripts/editor_manifest_check.py` 或相关测试

### 7.1 评审清单(不可机判项——列清单,不假装门禁)

以下判断静态硬化必假红,发生在 code review / 验收,不进门禁脚本:

1. **每个合成面运行时唯一滚动 owner**(容器R4 的全局形式;门禁只守三个点名文件)。
2. **新 overlay 组件入册**:新增全局浮层 primitive 时把文件加进 `css_scroll_affordance_check.PORTAL_REQUIRED_FILES`(滚动R6 只覆盖名册)。
3. **autoMediaAspect 按场景**:结果图浏览=真实比例、参考图/候选墙=稳定方卡、紧凑列表=定行高(thumb RULES 三场景);别把一个场景的正确行为当全局默认。
4. **预览 cap 值合比例**:容器R6 只断言 max-height 存在,值(如 3×row-h / 220px)是否恰当归评审;禁把魔数当基准繁殖。
5. **contentScroll 采纳**:已有内部 scroll-owner 的编辑器禁开 `contentScroll`(双滚动);mod 自定义内容无内部 owner 时才用。
6. **折叠 0×0 契约**(01-layout SPEC §2.10 C2d):mod 折叠面板必须 `display:none`/卸载,禁 width/height:0 视觉收起——运行时行为,浏览器验收 C6 项核。
7. **命令行分级**:哪个命令算高频常驻/哪个收进「更多」菜单(§3.4),ghost 出现点见 `zone_layout_check --json` 的 review_hints。
8. **mod 手写列表流的密度**:mod 自绘 role=list 的 stack 间距应对齐单列流 space-1 档(门禁只守 thumb-grid;mod 侧静态无零误报判据)。

禁止新代码新增:

- `form` 被描述为属性面板。
- `gallery` 作为新公共 editor class。
- 用户可切 LG/MD/SM/thumbSize cycle。
- `terminal` 作为独立 editor class。
- editor 之间直接互相 import。
