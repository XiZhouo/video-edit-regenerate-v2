# Editor layer philosophy

## 一句话

Editor 是 build v3 中区的工作面单元。它不代表某个具体 skill，也不代表一整套产品页面；它只负责把一类实体数据变成一种可看、可选、可编辑的工作面。

Editor 也不是"一个功能区"。摘要、筛选、日志、实体列表、工具命令、状态提示这些相关区域可以同属一个 editor;只要它们围绕同一批实体、同一个选择态和同一组命令闭环工作,就应该在 editor 内用 zones/body 组织,不要拆成多个 editor。

这句话决定了三条边界：

- editor core 提供稳定工作面：列表、属性、卡片、2D、3D、时间线、管线、审阅等。
- mod 提供业务意义：实体类型、字段含义、命令语义、算法、模型、管线、权限和产物。
- L5 LayoutTree 提供组合关系：多个 editor 同屏时靠布局和全局 selection 联动，不靠 editor 互相 import。

## 为什么要这样分

build v3 要解决的不是“做一个画图工具”或“做一个 pipeline 工具”，而是让不同 skill 能用同一套工作台骨架快速长出自己的工具。真正可复用的不是某个业务界面，而是这些基础工作动作：

- 看一组实体：table / cardlist / task / chart / graph / timeline / outline。
- 看一个实体或一个选中范围：property / text / code / asset / media / scene3d / review / compare / html / conversation。
- 在空间里操作实体：canvas2d。
- 复盘一次运行：pipeline。

所以 editor 的抽象不能按业务名字切，比如“抠图编辑器”“找物体编辑器”“生成候选编辑器”。这些应该是 mod 用 entity + command + layout 拼出来的 skill，而不是 core 里的 editor class。

## Editor core 包括什么

Editor core 包含四类东西。

第一类是 editor submodule：`editors/<id>/`。每个内置 editor 都有自己的文件夹，里面放：

- 代码入口：`<Name>Editor.tsx`。
- `README.md`：这个 editor 是什么，core 负责什么，不负责什么。
- `CONSUMPTION.md`：mod 要怎么声明 slot、`view_state`、字段映射和 command。
- `VALIDATION.md`：manifest、CSS、互嵌、写入边界要怎么验。

第二类是共享框架：`EditorFrame.tsx`、`types.ts`、`lib/dispatch.ts`、`lib/useEditorShortcuts.ts`、`lib/lazyModule.tsx`。这些东西定义 editor 怎么进壳、怎么派发命令、怎么读状态、怎么做局部快捷键。

第三类是共享 zone：`zones/`。zone 是 editor 内部的功能区，比如 HeaderZone、ToolRowZone、TableZone、GridZone、FormZone、PreviewZone、TimelineZone、GraphCanvasZone。editor 可以组合 zone，但不能重新发明一套顶部横栏。

第四类是共享 primitive/model：`primitives/` 和 `lib/*Model.ts`。它们是可复用的局部交互或纯逻辑，比如 viewport、gizmo、compare geometry、canvas2d layer model、card list model。primitive 不是 editor，也不能命名成 `*Editor.tsx`。

## Editor core 不包括什么

Editor core 不包括业务算法。比如图像修复、扩图、扣图、3D 重建、表格查询、向量搜索、模型调用、文件转码、权限审批都不属于 editor core。

Editor core 不包括业务状态机。运行态来自 command/pipeline/status store；editor 只显示和触发。

Editor core 不包括跨 editor 的私有连接。一个 editor 不能 import 另一个 editor。canvas2d 不能把 cardlist 当图层面板塞进自己，cardlist 也不能知道旁边是不是 canvas2d。

Editor core 不包括业务 CSS。mod 不应该复制 `.ze-*` 皮肤；缺共享能力时回到 `03-css-rules` 补系统规则。

## Mod 怎么消费 editor

mod 只通过 LayoutTree 消费 editor：

```ts
slot('flat-layers', 'cardlist', ['layer'], {
  layoutMode: 'compact-list',
  cardSize: 'compact',
  thumbField: 'assetRef',
  visibilityField: 'visible',
  lockedField: 'locked',
  reorderCommand: 'layer.reorder',
})

slot('layer-tree', 'outline', ['layer'], {
  parentField: 'parentId',
  orderField: 'order',
  thumbField: 'assetRef',
  visibilityField: 'visible',
  lockedField: 'locked',
  badgeField: 'role',
})

slot('canvas', 'canvas2d', ['layer'], {
  preset: 'ps-lite',
  layerType: 'layer',
  bgField: 'sourceRef',
  paintCommand: 'artwork.paint_layer',
})

slot('props', 'property', ['layer'])
```

这三块一起看起来像“2D 图层编辑器”，但它不是一个巨大的专用 editor。它是 L5 组合：

- cardlist 负责 flat card/list 形态的 layer 实体列表。
- outline 负责 tree/group 形态的 layer 实体层级。
- canvas2d 负责把 layer 实体投影到 2D 工作面。
- property 负责编辑当前 layer 的字段、只读信息、引用关系和控制命令。
- 三者通过 `selection['layer']` 联动。

这才是 build v3 的通用逻辑：editor 专心做一种工作面，skill 用布局把工作面拼成产品。

这条规则不是说"每个功能区都做成一个 editor"。拆 editor 的理由只有工作面真的不同:例如空间画布、实体集合列表、当前选中对象详情、运行链路复盘。普通搜索、排序、检测摘要、日志、导入导出和批量命令只是当前 editor 的功能区。

## 什么时候拆 editor

默认一个业务工作台用 1-3 个 editor;超过 4 个 editor 必须能说明每个 editor 都是独立工作面,否则应合并成功能区。拆 editor 只看四件事:

- **工作面不同**:画布、列表、属性、pipeline/notebook、媒体预览这类交互模型不同。
- **选择域不同**:一个 editor 管实体集合(master),另一个 editor 管当前选中实体或选中范围(detail)。
- **滚动/viewport 独立**:canvas/media/scene3d 这类硬 viewport 与 rows/form 这类内容流不能混成一个滚动者。
- **用户需要同时对照**:两个完整工作面需要并排比较或互相参照。

不拆 editor 的情况也要写死:同一实体集合的摘要、筛选、排序、问题日志、批量操作、导入导出和列表本身是一组相关功能区,应留在一个 editor 里。功能区的形式靠 role、布局、rows/card flow、分隔、选中态和滚动归属组织,不是靠每块套 raised panel/unit/card。

## Custom Renderer 不是结构逃逸口

`editor:'custom' + rendererKey` 是业务面板的逃生舱,不是跳过编辑器语法的许可。custom renderer 仍然必须继承 build v3 的工作面分工:

- 一组实体的浏览、筛选、选择 = master 角色,视觉上按 `cardlist/table/outline` 的规则组织。
- 当前选中实体的详情、字段、只读信息、命令 = detail 角色,视觉上按 `property + InfoControlUnit` 的规则组织。
- 如果产品必须把 master 和 detail 放在同一个 custom pane 内,只能做成「一个列表/内容流滚动归属 + 一个当前选中 entity 的 unit」。一组实体、检测日志、摘要统计默认按 rows/card flow/summary strip 组织,不能每段都套 `.ze-editor__unit`。
- custom renderer 不能因为有多个功能区就拆多个 editor。先判断是否同一工作闭环;是,就在一个 editor 内按 zones/body 排。
- 默认详情只放当前任务最关键的闭环。长字段、技术字段、JSON、prompt 和实现引用必须进入语义分组,不按 schema 顺序全量摊开。
- 折叠组用 `.ze-disclosure` / `Collapsible` 语义,不得在 custom renderer 中直接使用原生 `details/summary` 来替代系统折叠头。
- `.ze-field-row` 是整行字段原子,带固定 label 列和 row-height。不得把多个 `.ze-field-row` 放进半宽 `.ze-grid[data-cols="2"]` 里做字段矩阵;窄面板用单列字段流,宽面板优先用真正的 `property` slot。
- 类型相关字段必须按类型显隐,例如 AI Prompt 只在 AI evaluator 下出现;不要用空 textarea 证明字段存在。
- custom renderer 必须配 skill-local validation,至少守住:无裸 `details/summary`、无 `ze-grid` 套 field-row、默认详情不是 schema dump、滚动/fade 归属符合 zone 规则。

## 图层消费边界

标注本身就是图层；mask、参考图、候选图、输出图也一样。core 只把它们当成
`layer` / `entity` / `asset_ref` / `metadata` 来看：

- canvas2d 负责可见、锁定、选择、绘制、变换、合成、导入导出等通用 2D 工作动作。
- cardlist 负责 flat 图层/资产/候选卡片列表。
- outline 负责树状图层、分组、折叠、reparent 和行级通用操作。
- property 负责当前选中实体或选中范围的属性面板。
- pipeline 负责运行态和产物展示。

某个 skill 把 `role: "annotation"` 的层解释成提示词标注、把像素层解释成 mask、把
候选图解释成可采纳结果，都是该 skill 的 command/pipeline 消费逻辑。core 不写“发送给
AI 的标注模式”、不内置 prompt 规则、不决定哪些层参与生成，也不做业务 pasteback。
多个 editor 同屏时只通过 selection 联动；cardlist/outline/property/canvas2d 互不内嵌。

## 2D editor 的核心理解

canvas2d 是通用 2D 工作面，不是某个业务模式集合。

它的基础对象是：

- 视口：平移、缩放、尺寸、坐标换算。
- 背景：一个 asset_ref 或实体字段解析出的图片。
- 实体几何：x/y/w/h/rotation 或 geometryField 映射成可拖拽框。
- 图层实体：`layerType` 指向的实体桶，每个实体是一层。
- 像素资产：某个 layer 或字段里保存的 PNG/JPEG/WEBP 引用。
- 命令接缝：新增、更新、删除、导入、绘制、导出、运行动作都 dispatch command。

这里没有一个独立叫 mask 或 annotation 的业务状态。背景、物体、粗选范围、透明图、后处理范围、红框标注、文字说明都只是 layer 或字段里的像素资产/几何/文本/metadata。某个 skill 后面把这张像素图当遮罩、范围、alpha、权重图、选择区，把某个红框当 prompt 区域去算，是 mod 的算法语义，不是 core 的模式语义。

因此 canvas2d 的正确表达是：

```ts
slot('range', 'canvas2d', ['asset'], {
  bgField: 'imageRef',
  selectionLayerField: 'rangeLayerRef',
  tools: ['brush', 'eraser'],
  pixelLayerCommand: 'asset.update_range_layer',
})
```

`pixelLayerField` 是“我正在编辑一张可写像素层”。`selectionLayerField` 是“这张像素层会被 mod 当成选择范围消费”。`pixelLayerCommand` 是提交这张像素层的命令。`maskField`、`rangeMaskField`、`maskCommand`、`maskSelection` 只是历史 alias，新设计不能再把它们当成 core 概念。

canvas2d 内部固定分成两个功能区：

- 画布工具功能区：笔、橡皮、笔刷参数、撤销/重做、清除、运行按钮等通用工具。
- 2D 画布功能区：底图、视口、图层投影、移动/缩放/旋转、像素采样、笔画合成、PNG 提交。

工具功能区只决定“用户现在怎么改这张图”。2D 画布功能区只负责“把修改后的层准确写回字段/实体”。至于这张层是不是后续当 mask、range、alpha、lock map，是 mod 命令和管线的消费规则。

## 顶部横栏的统一原则

每个 editor 都必须长得像同一套工作台里的同类容器。

- header 是身份栏：标题、类型、数量、轻量状态。
- statusbar 是状态栏：运行状态、选区、辅助信息。
- 操作按钮属于 work 区里的功能区，例如 ToolRowZone、ControlZone、ActionBarZone；默认是 control group 承载槽,不额外套同款四边块。
- 横栏不是功能区块，不能做成 raised card。
- 功能区不是横栏，必须在 work 区里按 zone 规则和边界语法出现；canvas/preview/content 这类主工作面必须保留 viewport 边界。
- **工具行（ToolRowZone）的模式/视图控件默认隐藏或自解释**：CardList 卡片尺寸以 mod `cardSize` preset 为默认,运行时只能显示明确的无级“缩略图尺寸”滑杆,不得裸显 "MD"/"SM" 循环;compare 等非通用控件 **opt-in**（mod `view_state.compare:true` 才渲），绝不在每个 cardlist 顶部塞裸缩写或常灰开关——否则用户看不懂这行是什么（2026-06 用户反馈「MD / 对比 / SM 那行看不懂」）。

2D editor 现在看起来相对正确，就是因为它把身份、工具、画布、状态拆开了。其它 editor 的 toolbar 迁移也应该向这个结构靠，而不是各写各的顶部条。

## 写入原则

editor 不直接改 document store。所有写入走 command：

- 字段修改：`entity.update` 或 mod 声明的专用 command。
- 新增实体：`entity.add` 或 mod 声明的 add command。
- 删除实体：`entity.delete` 或 mod 声明的 delete command。
- 重排、导入、绘制、导出、运行：必须由 mod 声明 command。

payload 必须带足够的实体定位信息，常规写入需要 `type/id/expected_version`。这样 undo、CAS、审计、协作、运行态才不会断。

## Selection 原则

跨 editor 联动只通过全局 selection：

- table/cardlist/task/timeline/graph/outline 是 master，它们负责选择实体。
- property/asset/media/text/code/review/compare/html/conversation 是 detail，它们读当前选中实体或选中范围。
- canvas2d 在几何模式下既可以选择实体，也可以在 layerType 模式下读写 `selection[layerType]`。
- pipeline 用 selected entity 和 run 映射规则决定当前 notebook。

一个 editor 不能直接调用另一个 editor 的 setter。能通过 selection 表达的，就不要造私有连接。

## 空态原则

editor 外壳（EditorFrame：header + 边框 + 内容区）**恒在**，空态只在内容区：

- master 编辑器（cardlist/table…）空列表时仍渲外壳，空提示在内容区（已如此）。
- detail 编辑器（property/asset…）**无选中实体时同样外壳恒在**，内容区显「选一个 X 编辑」——**绝不整槽塌成一行裸占位**（2026-06 用户反馈「没选中对象时属性面板整块消失 = 完全不对」）。
- 实现：detail 编辑器组件收 `entity: EntityDict | null`，null 时自渲空骨架；装配层（slotAdapters）不在外面早返回占位 div。

## Validation 原则

结构约束必须机器化。`scripts/editor_manifest_check.py` 是 editor 层的红绿线：

- 注册表里的非 null editor 必须在 `EDITOR_MANIFEST.json` 声明。
- adopted editor 必须有 fixture、functional zones、builtin interactions。
- manifest 里声明的 selector 必须在 CSS 里存在。
- row-h editor 的行高必须使用系统 token。
- editor 实现不能 import 其它 `*Editor.tsx`。
- primitives 不能出现 `*Editor.tsx` 命名。
- `editors/<id>/` 必须存在，并且有代码、README、CONSUMPTION、VALIDATION。
- noRawToolbar editor 必须显式声明 sourceFile，且源码不能出现裸 `.ze-editor__toolbar`。

文档不是装饰。文档写下来的边界，必须被 manifest check 和测试尽量钉住。

## 功能区(zone)统一模型 — 编辑器 = 功能区的组合(M0)

编辑器壳 = `header? + zones[]… + content + statusbar?`(EditorFrame 画死)。功能区是可组合原子；编辑器是它们的组合。

**控制侧只有三个 data-role 角色**（统一收敛）：
- `filterbar` —— **辅助查看**：搜索 + 排序 + 状态 pill 筛选。改 view_state，即时、可逆、**无 confirm**、不派发命令。（FilterBarZone）
- `control` —— **触发命令**：工具/模式切换 + 全局/选区命令 + **管理动作**（删除/清空/反选/全选,即 scope:selection）。dispatch,有 confirm/busy 守卫。「工具功能区 ≡ 控制功能区」「管理功能区 = control 的 scope:selection 用法」——都不是独立角色。（ControlZone;`toolbar`/`canvas-toolbar`/`actionbar` 正收敛进 control,各组件仍出 `data-role=control`）
- `transport` —— 播放控制（ref 回调驱动原生媒体 API,非命令 dispatch;唯一保留的控制侧特例）。

`unit`（信息区 `data-role=info` + 控制区 `data-role=control` 二分）是内容块,不是顶层角色 —— 属性面板/管线节点由 N 个 unit 堆叠搭成。`header`/`statusbar` 是 chrome 横栏。

**横条三布局模式**（`data-layout`,纯 CSS 无 JS 量算）：
- `hug-scroll` —— 单行贴合,超宽横向隐滚,**不占满**（命令多不想拉伸时）。
- `wrap-cluster` —— 换行成簇,每项 hug 宽,**不拉伸**（pill 筛选簇）。
- `flex-fill` —— **换行 + 按比例拉伸占满当前行宽**（用户「横条律」：左右占满 + `data-flex` 比例 + `min-width` 换行阈值 → 每行项数/行数随条宽动态涌现）。子项几何令牌 `--ze-bar-item-min/basis`。

## 命名规范

| 轴 | 规则 | 例 |
|---|---|---|
| 编辑器 class id | 小写单名词、是工作面非业务;新公共名优先 | `cardlist` `property` `canvas2d` `pipeline` |
| 编辑器 CSS 根 | `.ze-editor--<class>`(EditorFrame 画死;legacy alias 可映射旧 CSS) | `.ze-editor--cardlist` |
| 功能区 id = data-role | role 即 id,唯一一套词表 | `data-role="control"` |
| 功能区组件 | **一个 role 一个 `<Role>Zone` 组件**(禁碎裂、禁 inline 手写 `<div className="ze-editor__zone">`;现有角色放不下时用 core `FunctionZone` 兜底) | `FilterBarZone` `ControlZone` `FunctionZone` |
| view_state 键 | camelCase,后缀按种类:`*Field`/`*Command`/`*Mode`·`*Size` | `thumbField` `reorderCommand` `cardSize` |
| CSS 类 | `.ze-editor__<part>` BEM;布局 `data-layout`、角色 `data-role`、密度 `data-lines`/`data-size` | `.ze-editor__zone[data-layout="flex-fill"]` |
| 命令 id | `<entityType\|域>.<动词>` snake | `entity.delete` `layer.reorder` |

## 迭代规则(保飞轮运转)

1. **新功能区** = 新 `data-role`(`zones/types.ts` + `03-zones/SPEC.md`) 或新 `data-layout`(L2 `editor-shell.css` + 受限布局集)。永远走 `<Role>Zone`(一 role 一组件),现有角色过渡期只能走 core `FunctionZone`,绝不在编辑器里手写 inline zone。在 `EDITOR_MANIFEST.functionalZones[]` 声明且选择器在 CSS 可解析。链条:`types.ts → SPEC → EDITOR_MANIFEST → editor_manifest_check + editor_frame_check + css_layer_guard`,缺一处门禁红。
2. **新编辑器类** = `EditorClass` 联合 + `EDITOR_CLASSES` 注册 + `slotAdapters` 加 `case`(在 `case 'custom'` 前) + manifest + CONSUMPTION.md + fixture + 本文「五动作」;LayoutRenderer 不改(开闭)。
3. **展示取舍/控制 = 声明式 mod 配置喂给通用 core 编辑器**,绝不在编辑器加业务代码路径;不行就扩闭集 parser 加枚举成员(未知值→null,老配置仍绿);缺运行时行为加最小 supervisor 旗标,绝不引入 DAG。
4. **状态只在机器源**(manifest/`__all__`/`_contract`/lock),散文不写过期状态;零真消费者的编辑器类标 `status` 而非假装 adopted(候选门禁 `EDITOR_NO_REAL_CONSUMER`)。
5. **改受管层** = `_contract` bump(加性 MINOR)+ relock + 当日 `skill_sync` 全层下发 + 各下游 verify_all + 重部署;css 是 opt-in 层(定制皮肤不强制同步)。
