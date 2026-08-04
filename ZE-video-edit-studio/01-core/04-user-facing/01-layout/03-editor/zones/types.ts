/**
 * zones/types.ts — 功能区(functional zone)组件契约
 *
 * 协议: 03-css-rules/03-zones/SPEC.md。
 *
 * 设计铁律(用户要求的"拓展点配套规则"):
 *   zone = **固定骨架**(组件内部画死,调用方不可改)+ **受约束的拓展点**(props)。
 *   拓展点只接受 ①字段映射(data 字段名)②命令 id ③闭集枚举 ④i18n key;
 *   **绝不接受裸 onClick / 裸 ReactNode** —— 业务语义全在 mod 声明的命令+字段映射里,
 *   framework 零业务逻辑(editor-architecture 规则 13)。
 *
 * 声明式组合走路线 B:editor 组件内用 TS 显式组合 zone(编译期类型安全),
 * 不让 manifest JSON 驱动运行时。mod 的覆盖经 EditorSlotNode.zone_overrides(ZoneOverrides)
 * 在装配缝 slotAdapters 消费。
 */

import type { ReactNode } from 'react'
import type { FieldSchema, DispatchFn, FormActionResultSpec } from '../types'

// ============================================================
// zone 身份(与 EDITOR_MANIFEST.json functionalZones[].id / SPEC §2 词表一致)
// ============================================================
//
// 结构类闭集权威 = 03-css-rules/03-zones/ZONE-CLASS-REGISTRY.md(8 结构类)。
// 下面的 zone id 是**实现层名**;每个 id 归属一个结构类(结构类 = UI 结构模式,不是位置/业务名)。
// 归属映射(id → 结构类):
//   manage/filterbar → 控制条类(control-bar / ManageZone 子角色)
//   tool/control/toolbar/canvas-toolbar/actionbar/transport/context-menu → 控制条类(ToolZone 子角色)
//   statusbar → 状态区类(status-area / StatusZone);inspector → 字段组类(field-group / property)
//   list/grid/table → 列表类(list);form → 字段组类(field-group)
//   canvas/preview → 视口类(viewport);outliner → 树类(tree);text → 输入区/内容文本
//   compare/chart/timeline → 见下(compare/chart 零实例,timeline 专用)
// 收敛只在"结构类/public 口径"层发生;这些实现层名保留(既有 CSS selector + data-role + gallery 契约在用),
// 不删不改,故与 SPEC 的 管理/工具/内容 三口径不冲突——本 union 是它们的实现细分。

/** 操作区:触发动作 / 编辑参数,不承载主数据 → 全部归 **控制条类**(见 ZONE-CLASS-REGISTRY §B1)。
 *  新标准 public 口径 = `manage`(搜索/筛选/排序/选择管理,辅助查看和管理实体集合)
 *  + `tool`(工具/command/run/retry/paint/select/transform 等,对当前对象做什么)。
 *  兼容层(实现层 data-role,仍在用):旧 `filterbar`→manage 子角色;
 *  旧 `control`/`toolbar`/`canvas-toolbar`/`actionbar`/`transport`/`context-menu`→tool 子角色;
 *  `statusbar`→状态区类;`inspector`→字段组类。CSS 保留 data-role=filterbar/control/toolbar 给既有选择器和门禁。 */
export type ActionZoneId =
  | 'toolbar' | 'canvas-toolbar' | 'inspector' | 'transport' | 'context-menu' | 'statusbar' | 'actionbar'
  | 'filterbar' | 'control' | 'manage' | 'tool'
/** 内容区:展示 / 编辑主数据 → 归 **列表类/字段组类/视口类/树类**(见 ZONE-CLASS-REGISTRY §C)。
 *  list/grid/table→列表类;form→字段组类;canvas/preview→视口类;outliner→树类;text→内容文本。
 *  ⊘ 零实例(保留占位,不实现 CSS):`compare`(对比视口——已砍,AI 预览走就地覆盖非并排)、
 *  `chart`(规划中);`timeline` 为 timeline editor 专用。真启用前不算闭集实类。 */
export type ContentZoneId =
  | 'list' | 'grid' | 'table' | 'canvas' | 'preview' | 'compare'
  | 'timeline' | 'text' | 'form' | 'chart' | 'outliner'
export type ZoneId = ActionZoneId | ContentZoneId

/** zone 在 EditorFrame 里的落位。 */
export type ZoneRegion = 'header' | 'body' | 'footer' | 'aside' | 'overlay'

// ============================================================
// 拓展点元件(声明式、受约束 —— 这是各 zone 的"变化部分")
// ============================================================

/** 操作条的一个动作项。kind 决定语义,不允许逃逸成裸 onClick。 */
export interface ToolbarAction {
  id: string
  /** i18n key(chrome 文案,走 useT);禁裸文案 —— i18n 门禁约束。 */
  labelKey: string
  /** command=派发命令 / toggle=切视图态 / search=搜索框 / cycle=闭集档位循环钮。 */
  kind: 'command' | 'toggle' | 'search' | 'cycle'
  /** kind==='command' 必填:派发的命令 id。 */
  command?: string
  /** kind==='command':命令的固定参数(实体上下文由装配层补)。 */
  args?: Record<string, unknown>
  /** kind==='toggle'|'search'|'cycle' 必填:读写 viewState 的键。 */
  viewKey?: string
  /** kind==='cycle' 必填:档位闭集(显示原值,语言中立,如 'sm'|'md'|'lg');点击循环到下一档。 */
  options?: string[]
  /** 图标:图库名(icons.md 名册,渲染 token 图标)或任意短字符(glyph 直显,降级);双轨见 lib/zeIcon.IconOrGlyph。 */
  icon?: string
  variant?: 'primary' | 'ghost' | 'danger'
  /** 置灰(声明式布尔,编辑器渲染时算;非回调)。 */
  disabled?: boolean
  /** 前置可见:命令需要的非空 data 字段(沿用装配层 requiresFields 机制)。 */
  requiresFields?: string[]
}

/** 卡片列表编辑器动作区的一项(mod 声明)。动作文案=命令动词=可译 chrome:优先 labelKey(走 t()),
 *  回退 label(语言中立直显,兼容旧声明);confirm 同理 confirmKey?>confirm(string=文案/true=无文案守卫)。
 *  (labelKey?+label? 二选一沿用 StatusSegment/ListColumn 先例。)
 *  scope='selection' → dispatch(command, {type, ids:[...当前选中]}),按钮常驻;无选中时由编辑器拦截行为;
 *  scope='global'    → dispatch(command, {type})。 */
export interface GalleryAction {
  command: string
  labelKey?: string
  label?: string
  scope: 'selection' | 'global'
  confirmKey?: string
  confirm?: string | boolean
  /** 缺省 secondary(命令按钮静止可见轮廓);primary 一屏至多一个;ghost 仅低危高频辅助;danger 破坏性。 */
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger'
  /** 设了 = 点击先弹出该命令 params_schema 驱动的参数小窗(收集后再 dispatch);用于「批量生成」这类
   *  全局动作需用户填 count/主题/模式(贴 v1 列表工具条的生成弹窗)。scope 一般为 'global'。 */
  withParams?: boolean
  /** 声明式分组(行>组>件):同 cluster 值的动作归同一 .ze-row 组(组序=首现顺序);
   *  缺省走命令名推断(generate/disposition/export/other/danger)。registry-driven,优先于 if-链推断。 */
  cluster?: string
  /** 教学锚点 id。设了就在按钮上落 `data-ze-control="<id>"`,引导步的 `control:<id>` 便能指到它。
   *
   *  为什么要显式声明而不是拿 command 当 id:教学讲的是**语义位置**(「交付」这一步),
   *  而承担它的命令可能改名或拆成两个。语义 id 与命令 id 解耦,教学脚本才不会因为重构就失准。
   *  没声明就不落属性 —— 引导解析不到时优雅降级成「只说不指」(GuideLayer 既有行为)。 */
  controlId?: string
}

/** form 编辑器动作条(actionbar zone)的一项(mod 声明)。P-4 闭集:命令 id + 字段名 + 枚举,无回调。
 *  动作文案=命令动词=可译 chrome:优先 labelKey(走 t()),回退 label(语言中立直显,兼容旧声明);
 *  确认文案 confirmKey?>confirm(confirm: string=文案守卫 / true=无文案守卫;沿用 StatusSegment labelKey?+label? 先例)。
 *  scope='single'(缺省)→ dispatch(command, {type, id:active 实体});
 *  scope='selection'    → dispatch(command, {type, ids:[...当前多选]}),无多选时退化为 [active]。
 *  requiresFields:前置非空字段;active 实体里有空字段 → 置灰 + tooltip(轨①前置可见)。 */
export interface FormAction {
  command: string
  labelKey?: string
  label?: string
  scope?: 'single' | 'selection'
  confirmKey?: string
  confirm?: string | boolean
  requiresFields?: string[]
  /** 字段值显隐条件;如 {role:['candidate','output']}。不满足时按钮不渲染。 */
  visibleWhen?: Record<string, string | string[]>
  variant?: 'primary' | 'ghost' | 'danger'
  /** true = 该动作会删除、采纳或替换当前运行目标，pending/running 期间仅禁这一项。
   *  缺省时 danger 动作按冲突处理；false 可声明“仅视觉危险但不碰运行目标”。内置
   *  entity.delete 永远按冲突处理，不能覆盖。 */
  conflictsWithRun?: boolean
  /** 点击先开一个已注册的输入弹窗(dialogRegistry 的 key,如 'notify.push'),由弹窗收集参数后再 dispatch;
   *  设了 dialog 时忽略直发/confirm(弹窗自管确认)。填补「命令需用户输入」的通用接缝。 */
  dialog?: string
  /** 命令成功后的框架消费方式。闭集只允许 download；文件名来自响应中的耐久 Operation，
   *  本声明不得携带 URL/path/file/mime。dialog 自管 dispatch，故不能与 result 同时使用。 */
  result?: FormActionResultSpec
}

/** property 信息区里的声明式关键控件。用于“傻瓜参数面板”这类只暴露少量有效参数的工作流。
 *  path 默认相对 entity.data；也兼容 data.foo / <entityType>.foo 前缀。无 command 时由 core
 *  生成 entity.update 顶层 patch；有 command 时派发 {type,id,path,value,expected_version,...commandArgs}。 */
export interface InspectorControlSpec {
  key: string
  labelKey?: string
  label?: string
  path: string
  type: 'text' | 'number' | 'integer' | 'boolean' | 'select' | 'color'
  options?: Array<string | { value: string; label?: string; labelKey?: string }>
  min?: number
  max?: number
  step?: number
  command?: string
  commandArgs?: Record<string, unknown>
  valueArg?: string
  targetArg?: string
  pathArg?: string
  disabled?: boolean
  /** command 型控件会替换运行目标时显式声明；普通 entity.update 参数在运行中仍可编辑。 */
  conflictsWithRun?: boolean
}

/** 状态条的一个状态段(只显示,不放业务按钮 —— 职责≠操作条)。 */
export interface StatusSegment {
  id: string
  kind: 'badge' | 'count' | 'progress'
  /** chrome 文案走 labelKey(i18n);数据显示名走 label(语言中立,直显)。二选一。 */
  labelKey?: string
  label?: string
  value?: number | string
  variant?: 'info' | 'warn' | 'danger'
}

/** 列表/表格的列声明。format 是闭集枚举(非任意渲染函数)→ framework 零业务逻辑。 */
export interface ListColumn {
  field: string
  labelKey?: string
  label?: string
  align?: 'start' | 'end'
  format?: 'text' | 'bool' | 'number' | 'json'
  sortable?: boolean
}

/** 行/卡片操作:右键菜单项 + 行内动作,声明式。kind 闭集。 */
export interface RowAction {
  id: string
  labelKey: string
  kind: 'command' | 'rename' | 'delete' | 'adopt'
  /** kind==='command'|'adopt' 必填。 */
  command?: string
  args?: Record<string, unknown>
  /** kind==='rename' 必填:重命名目标字段。 */
  field?: string
  variant?: 'danger'
  placement: 'row-inline' | 'context-menu'
}

/** 网格 filter-strip 的一项(grid 的强制拓展点)。 */
export interface GridFilter {
  id: string
  labelKey: string
  /** 过滤依据的 data 字段。 */
  field: string
  /** 可选值集合(无则按字段去重派生)。 */
  values?: string[]
  /** 枚举值 → i18n key 的显示译名(如 'pending' → 'mod.genStatus.pending')。缺省/某值未映射时回退裸值。
   *  避免 pending/unreviewed 等机器枚举裸露在筛选条上,中英环境都读得懂。 */
  valueLabels?: Record<string, string>
}

/** CardList 排序声明(ManageZone 使用):字段映射 + 闭集比较类型 + 可选枚举顺序。
 *  纯视图态,不派发命令、不改数据;direction 缺省 asc,valueType 缺省 natural。 */
export interface CardSort {
  id: string
  labelKey?: string
  label?: string
  /** 排序依据:entity 顶层字段(name/id/version...)或 data 字段;支持 data.xxx 路径。 */
  field: string
  valueType?: 'string' | 'number' | 'natural' | 'enum' | 'boolean'
  direction?: 'asc' | 'desc'
  /** valueType='enum' 时按此顺序排序;未列值排在已列值之后。 */
  order?: string[]
}

/** inspector 分区声明 = 一个「信息+控制单元」(信息区[data-role=info] + 控制区[data-role=control]
 *  固定二分;控制收一处、信息区不夹命令)。kind 闭集;内容受 schema 约束,不接受任意 JSX。
 *  kind:fields/readonly-kv/params/controls/preview/variants/asset
 *    - preview  = 把某 asset_ref 字段渲为只读图片预览
 *    - variants = 当前实体的「副本/候选」子实体缩略图选择条(点一张=采纳,childType 子桶按
 *      childParentField 关联当前实体 id;复用 ThumbnailGrid primitive。「一个实体有多个副本,
 *      选一种」的属性面板内嵌选择,不另起 gallery editor)。 */
export interface InspectorSectionSpec {
  id: string
  titleKey: string
  /** mod 数据直显标题(语言中立;有则优先于 titleKey 走 i18n。GalleryAction.label 先例)。 */
  title?: string
  defaultOpen?: boolean
  /** 单元标题可折叠(收起信息区;缺省/false=常驻)。 */
  collapsible?: boolean
  kind: 'fields' | 'readonly-kv' | 'params' | 'controls' | 'preview' | 'variants' | 'asset'
  /** kind==='fields'|'readonly-kv':schema.properties 的字段名子集。 */
  fields?: string[]
  /** kind==='params':走 ParamPanel 的字段 schema 列表。 */
  paramSchema?: FieldSchema[]
  /** kind==='controls':声明式关键控件列表。 */
  controls?: InspectorControlSpec[]
  /** kind==='preview':要渲成图片预览的 asset_ref 字段名(只读;经 assetUrl 解析)。 */
  previewField?: string
  /** kind==='asset':可上传/挂载的 asset_ref 字段名(经 uploadAsset 写、AssetView 按 mime 预览;清除=单图写 null / 多图写 [])。 */
  assetField?: string
  /** kind==='asset':接受格式(原生 file input accept;如 'image/*'、'video/*,.glb';省=任意)。 */
  accept?: string
  /** kind==='asset':多资产模式(写 asset_ref[]);缺省由字段 schema array<asset_ref> 自动推断。 */
  multiple?: boolean
  // ── kind==='variants' 拓展点(闭集字段名 + 命令 id,无回调) ──
  /** 副本/候选子实体类型(如 'object_candidate')。装配层按它从 entitiesByType 取桶。 */
  childType?: string
  /** 子实体里指向当前实体 id 的外键字段(如 'objectId');装配层据此过滤出本实体的副本。 */
  childParentField?: string
  /** 子实体缩略图 asset_ref 字段(缺省 'imageRef')。 */
  childThumbField?: string
  /** 子实体评分字段(可选;显示在卡角)。 */
  childScoreField?: string
  /** 当前实体上记录「已采纳子实体 id」的字段(如 'adoptedCandidateId');用于高亮当前选中卡。 */
  adoptedField?: string
  /** 点一张子实体=采纳,派发的命令(dispatch {type:childType, id})。 */
  adoptCommand?: string
  /** 删除一张子实体的命令(可选;dispatch {type:childType, ids:[id]})。 */
  childDeleteCommand?: string
  /** ★控制区:本单元的命令钮组(复用 FormAction 闭集,不新增类型;无回调)。 */
  actions?: FormAction[]
}

// ============================================================
// zone 组件 props 契约(固定骨架由组件画死,这里只声明拓展点)
// ============================================================

/** toolbar 操作条。固定:title+count+actions 容器+溢出收敛。拓展点:actions。 */
export interface ToolbarZoneProps {
  /** 数据显示名(typeLabel,语言中立,直显)。 */
  title?: string
  /** 计数 / 元信息槽:仅显示用(数字 / 文本 / 只读指示器如进度环/状态徽章);非交互拓展点。 */
  count?: ReactNode
  /** 拓展点:右侧动作组(声明式,受 ToolbarAction 约束)。 */
  actions?: ToolbarAction[]
  dispatch?: DispatchFn
  /** toggle/search 类 action 的视图态读写(编辑器持有,frame 透传)。 */
  viewState?: Record<string, unknown>
  onViewChange?: (key: string, val: unknown) => void
}

/** statusbar 状态条。固定:单行+collapse 把手+瞬时渲染。拓展点:segments。 */
export interface StatusZoneProps {
  running: number
  queued: number
  latestOp?: string
  onCancel?: () => void
  /** 拓展点:额外状态段(领域计数,如"已采纳 3");禁业务按钮。 */
  segments?: StatusSegment[]
}

// 注:list/table 用 TableZone(zones/TableZone.tsx 定义 TableZoneProps),grid 用 GridZone
// (zones/GridZone.tsx),inspector 走 InspectorSection/ParamPanel primitive —— 各组件自带 props,
// 复用上面的元件类型(ListColumn/RowAction/GridFilter/InspectorSectionSpec);此处不再重复声明抽象 props。

// ============================================================
// mod 重载点(EditorSlotNode.zone_overrides 收紧为此;装配缝 slotAdapters 消费)
// ============================================================

/** mod 经 layout 声明式覆盖某 editor 槽的 zone(可见性 + 追加拓展点项);core 零改。 */
export interface ZoneOverrides {
  /** 隐藏/显示某 zone(可关闭 zone:statusbar/inspector)。 */
  visibility?: Partial<Record<ZoneId, boolean>>
  /** 给某 zone 追加拓展点项(mod 声明,如给 toolbar 加命令、给 list 加列)。 */
  extend?: {
    toolbar?: ToolbarAction[]
    statusbar?: StatusSegment[]
    list?: { columns?: ListColumn[]; rowActions?: RowAction[] }
    grid?: { filters?: GridFilter[]; cardActions?: RowAction[] }
    inspector?: { sections?: InspectorSectionSpec[] }
  }
}
