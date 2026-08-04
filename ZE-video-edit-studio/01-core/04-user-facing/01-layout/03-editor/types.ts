/**
 * 03-editor 类型契约 (M1)
 *
 * 协议: ./SPEC.md §2.4 (输入输出契约) + §6 (M1 接口签名)
 *
 * editor 是中区渲染单元, 受控件: props 进 / dispatch 出。entity 数据由上层 provider
 * (webapp 的 useDocument) 订 SSE 后重新喂 props 实现 SPEC §2.4 的 "subscribe";
 * editor 本身不直接订阅、不直接 mutate entity。
 */

/** Entity.to_dict() 的前端形态 (见 01-data-structure/02-entity Entity.to_dict)。 */
export type EntityDict = {
  id: string
  type: string
  name: string
  version: number
  deleted: boolean
  read_only: boolean
  data: Record<string, unknown>
  // provenance / actor_log / created_at / updated_at 略 (M1 editor 不渲染)
}

/** 写通道: 所有变更走 command dispatch (SPEC §2.4)。fire-and-forget 为主,
 *  父层据 dispatch 响应 / SSE 重新喂 entities props → 重渲染。
 *  返回值加宽为 void | Promise<unknown>(对齐 CustomPaneProps 既有形态;真实注入的
 *  dispatchCmd 返回 Promise<CommandResult|undefined>):结果型消费者(pipeline 操作台读
 *  op.error 的 requires_runtime / actionbar busy 态)可 await,既有 fire-and-forget 调用零改。 */
export type DispatchFn = (commandId: string, args: Record<string, unknown>) => void | Promise<unknown>

/** 声明式 FormAction 的结果消费闭集。mod 只能选择框架已实现的消费方式，不能注入
 * URL、路径、MIME 或任意回调。`download` 的文件身份来自已提交 Operation，下载端点由
 * App 固定，服务端再做 project / generation / member / filename 四重校验。 */
export type FormActionResultSpec = { kind: 'download' }
export type FormActionResultConsumer = (
  spec: FormActionResultSpec,
  result: unknown,
) => void | Promise<void>

/** field-types 的单字段 schema (子集; 完整见 04-field-types)。 */
export type FieldSchema = {
  type?: 'string' | 'number' | 'integer' | 'boolean' | 'array' | 'object' | string
  required?: boolean
  [k: string]: unknown
}

/** 一个 entity_type 的 object schema (doc.schema[entity_type])。
 *  label = 类型级显示名(数据显示名,非翻译 key;框架透传、mod 填,见 05-i18n/README『chrome vs 数据 边界』)。 */
export type EntityTypeSchema = {
  type?: string
  label?: string
  properties?: Record<string, FieldSchema>
  required?: string[]
}

/** table editor 的列声明。 */
export type Column = { field: string; label?: string }

/** entity_ref 选择器的一个候选项(装配层从 entitiesByType 按绑定类型 `to` 预过滤后派生:
 *  id=候选实体 id、label=显示名(name 回退 id))。 */
export type EntityRefOption = { id: string; label: string }
/** entity_ref 候选表:按字段绑定类型(schema `to`)分桶的候选实体项。装配层(slotAdapters)
 *  从 entitiesByType 预计算,逐层透传给 FieldRow;缺某类型 → 该字段降级为写 {"$ref"} 的文本输入。 */
export type EntityRefOptions = Record<string, EntityRefOption[]>

/**
 * default editor class (SPEC §2.2, 开集)。
 *   M1: table / form。
 *   Block2 落地: gallery(多实体缩略图,单一职责,与单资产 asset 区分)/ task / canvas2d /
 *               scene3d / asset / media。
 *   留 null 占位(mod 可 register,或 core 后补): timeline / text / code / graph / review。
 */
export type EditorClass =
  | 'canvas2d' | 'scene3d' | 'timeline' | 'keyframe-curve' | 'table' | 'form' | 'property' | 'text'
  | 'code' | 'media' | 'graph' | 'asset' | 'review' | 'compare' | 'task' | 'gallery' | 'cardlist' | 'chart'
  | 'pipeline' | 'outline' | 'html' | 'dialog' | 'conversation' | 'document'
  // 'custom' = mod 注入式自定义面板:没有 core 实装组件(值 null),靠 view_state.rendererKey
  // 从 ctx.renderers 取 mod 提供的组件渲染。机制通用、core 零业务(见 slotAdapters case 'custom')。
  | 'custom'

/** 容器断点 (SPEC §2.8; M1 支持 xs / md / xl 三档)。 */
export type Breakpoint = 'xs' | 'md' | 'xl'

/**
 * CustomPaneProps — slot editor:'custom' 时,core 透传给 mod 注入组件的契约(见 lib/slotAdapters case 'custom')。
 * mod 组件只靠这些 props 读数据/派命令,不触 core 内部。机制通用,任意 skill 可注入自定义面板。
 *
 * 跨类型选择读写不在本 props 上:props 只有宿主类型的 selectedId/onSelect。多类型面板一律用
 * `useEditorSelection()`(01-layout 导出,F8)读 selection 全表 / 写任意类型;不存在
 * props.selection / props.onSelectEntity(曾有、已删,直读直调 = 运行时崩溃)。
 */
export type CustomPaneProps = {
  /** 该 slot 绑定的主 entity 类型 */
  entityType: string
  /** 该类型的全部实体 */
  entities: EntityDict[]
  /** 当前选中 id(无则 null) */
  selectedId: string | null
  /** 当前选中实体(无则 null) */
  selected: EntityDict | null
  /** 全类型实体表(跨类型读) */
  entitiesByType: Record<string, EntityDict[]>
  /** 主类型 schema(字段 label 等) */
  schema?: EntityTypeSchema
  /** layout 里该 slot 的 view_state(含 rendererKey 及 mod 自定义键) */
  viewState: Record<string, unknown>
  /** 选中某实体 */
  onSelect: (id: string) => void
  /** 新建该类型实体(若框架提供) */
  onAdd?: () => void
  /** 派发命令(Command→Operation 唯一写路径) */
  dispatch: (cmd: string, payload?: Record<string, unknown>) => Promise<unknown> | unknown
  /** asset_ref → url(读图/视频) */
  assetUrl?: AssetUrlFn
  /** 上传二进制 → asset_ref。custom editor 需要把素材导入收束到自身 section 时使用。 */
  uploadAsset?: (bytes: Uint8Array, mime: string, ext?: string) => Promise<AssetRefDict>
  /** 取某实体运行状态(busy/进度)——curry 到宿主 slot 主类型 */
  statusOf?: (id: string) => EntityRunStatus | undefined
  /** 按 type+id 查任意实体运行状态(与 entitiesByType 跨类型读数据对称)。custom zone 派发到【非宿主类型】
   *  实体的 stage 命令(如对话锚 project_config),靠它读那条回合的 running/error;装配层透 ctx.statusFor。 */
  statusFor?: (entityType: string, id: string) => EntityRunStatus | undefined
}


// ============================================================
// 运行态 / 加载占位契约 (Block1 加载 / 管线 / 占位系统)
//
// entity 的运行状态是会话态 Rendering Hint(不进 Document / OpLog),由后端
// PipelineSupervisor 按文件系统派生、经 SSE entity_status 消息下发。editor 经
// StatusLookup 查某 entity 当前相位,渲染对应骨架 / 错误占位 / 进度环。
// 占位不是新系统:占位 = 处于已知相位(pending)的 entity,复用既有
// entity → editor → SSE 反应式流。
// ============================================================

/** entity 运行相位。pending=占位待跑;running=生成中(带 progress);done=已产出;error=失败可重试。 */
export type RunPhase = 'pending' | 'running' | 'done' | 'error' | 'skipped' | 'interrupted' | 'stale'

/** 失败信息(error 相位携带);trace_id 贯穿 UI→API→command→operation→oplog。 */
export type RunError = { code: string; message: string; trace_id?: string }

/** 多 stage item 内单个 stage 的运行态(后端 StageStatus.to_dict 形态;P2)。
 *  单 stage 运行态不带本结构(EntityRunStatus.stages 缺省)。 */
export type StageStatus = {
  stage_id: string
  label: string
  phase: RunPhase
  /** 0..1;null/undefined = indeterminate */
  progress?: number | null
  /** 剩余预估秒;null/undefined = 未知(ETA 由 P2d 填) */
  eta_sec?: number | null
  /** 吞吐;null/undefined = 未知 */
  throughput?: number | null
}

/** 单 entity 的运行态(会话态)。 */
export type EntityRunStatus = {
  phase: RunPhase
  /** Supervisor 在 prepare_start 实际新建实体时签发；普通/既有实体即使内容为空也省略。 */
  placeholder?: boolean
  /** 0..1;undefined = indeterminate(不确定进度)*/
  progress?: number
  /** 人类可读阶段名(骨架 / 进度环的 caption)*/
  stage_label?: string
  error?: RunError
  /** 归属的 pipeline run id(用于重试 dispatch)*/
  run_id?: string
  /** 多 stage 时各 stage 的运行态(单 stage 缺省 → 退回 entity.data 静态 stages)。 */
  stages?: StageStatus[]
  /** 当前/已完成 stage 序号(0-based;单 stage 缺省)。 */
  stage_index?: number
  /** 整个 item 剩余预估秒(单 stage / 未知缺省)。 */
  eta_sec?: number
}

/** 编辑器查某 entity 运行态的函数(经 SlotContext 注入,免穿透 props)。 */
export type StatusLookup = (entityType: string, entityId: string) => EntityRunStatus | undefined

/** 编辑器级粗粒度加载态(primitive 用:idle 正常 / loading 骨架 / error 占位)。 */
export type LoadingStatus = 'idle' | 'loading' | 'error'

/** asset 引用(entity.data 里的资产指针;按 sha256 内容寻址)。editor 经 AssetUrlFn 解析 URL。 */
export type AssetRefDict = {
  sha256: string
  mime?: string
  size?: number
  ext?: string
  [k: string]: unknown
}

/** 把 asset 引用解析成可加载 URL(由装配层注入;editor 不直读 AssetStore)。 */
export type AssetUrlFn = (ref: AssetRefDict) => string
