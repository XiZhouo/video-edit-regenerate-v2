/**
 * pipelineChain — 管线链路主视图纯逻辑(供 PipelineEditor 调用 + vitest)
 *
 * 管线编辑器的主模型不是日志堆,而是「对象组 → command → 对象组」的链路:
 *   [对象组 A] --command--> [对象组 B] --command--> [对象组 C]
 * 本模块只做声明式 view_state.chain 的解析与解算(纯数据,无 React):
 *   - parsePipelineChainConfig: 闭集校验 + 归一化;非法回退 null(旧视图照常)。
 *   - resolvePipelineObjectGroup: 对象组 source(闭集)→ 实体/快照。
 *   - resolveChainArg / resolveStepArgs: command 参数(闭集)→ payload + 缺位原因。
 *   - phaseFromSteps / phaseForMergedStages / progress / error: 合并多 stage 的运行态。
 *
 * 设计铁律(见 plans/property-pipeline-editor-redesign.md §6):对象组只描述「此处有什么」,
 * command 只描述「如何加工」;视觉一律复用既有 entity display,本模块不产任何 DOM。
 *
 * stage 标识:step.stage_id(非 command_id)。phaseForMergedStages 的 stageIds 必须传 stage_id,
 * 与 run.step_states 的 stage_id 对齐(command_id 是另一套命名空间)。
 */

import type { EntityDict, RunError } from '../types'
import type { PipelineStepState } from '../../02-footer'

function isRecord(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === 'object' && !Array.isArray(v)
}

function asString(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

function asStringArray(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
}

/** 版本感知的实体字段读取:id/name/type/version 读根字段,其余读 data[field]。
 *  (与 PipelineEditor.readEntityField 同义;ChainArgSpec 的 id|name|type|version 闭集靠它。) */
function readChainField(entity: EntityDict | null | undefined, field?: string): unknown {
  if (!entity || !field) return undefined
  if (field === 'id') return entity.id
  if (field === 'name') return entity.name
  if (field === 'type') return entity.type
  if (field === 'version') return entity.version
  return entity.data?.[field]
}

// ============================================================
// 闭集类型(对应 plan §7 public interface)
// ============================================================

export type PipelineGroupSource =
  | { kind: 'activeEntity' }
  | { kind: 'entityByField'; entityType: string; field: string }
  | { kind: 'entityHistory'; entityType?: string; keyField: string; runIdField?: string }
  | { kind: 'assetField'; field: string }
  | { kind: 'stageOutput'; stageId: string; field: string }
  // 单例:文档级唯一实体(如项目设置 project_config),与选择无关 —— 链路里「设置节点」用它。
  | { kind: 'singleton'; entityType: string }

export type EntityDisplaySpec =
  | { kind: 'preview'; field: string; fallbackField?: string }
  | { kind: 'previewKv'; previewField: string; fields: string[] }
  | { kind: 'fields'; fields: string[] }
  | { kind: 'kv'; fields: string[] }
  | { kind: 'assetPreviews'; fields: string[] }
  | { kind: 'thumbnailList'; thumbField: string; labelField?: string; metaFields?: string[] }
  | { kind: 'empty'; textKey?: string }
  // 可编辑表单:把链路节点的实体直接做成可改字段。fields 含标量+asset,经 FormZone 按字段 schema
  // 类型派生控件(标量→输入失焦 entity.update;asset_ref/format:asset→AssetRefInput 上传)。
  // 让管线编辑器既能「显示」也能「就地编辑」该节点实体(如项目设置)。
  | { kind: 'form'; fields: string[] }

export type ChainArgSpec =
  | string
  | number
  | boolean
  | null
  | { source: 'activeEntity'; field: string }
  | { source: 'groupEntity'; groupId: string; field: string }
  | { source: 'literal'; value: unknown }

export type PipelineObjectGroupSpec = {
  id: string
  /** 组标题=可译 chrome:labelKey(走 t())优先,回退 label(语言中立直显,兼容旧声明)。 */
  labelKey?: string
  label?: string
  source: PipelineGroupSource
  display: EntityDisplaySpec
}

export type PipelineCommandStepSpec = {
  id: string
  /** command 标题=可译 chrome:labelKey(走 t())优先,回退 label。 */
  labelKey?: string
  label: string
  from: string
  to: string
  commandId: string
  stageIds?: string[]
  args?: Record<string, ChainArgSpec>
  defaultOpen?: boolean
}

export type PipelineDetailsSpec = {
  defaultOpen?: boolean
  showStartConsole?: boolean
  showNotebook?: boolean
  showMatrix?: boolean
}

export type ParsedPipelineChain = {
  groups: PipelineObjectGroupSpec[]
  steps: PipelineCommandStepSpec[]
  details: Required<PipelineDetailsSpec>
}

// ============================================================
// 解析(parse):闭集校验 + 归一化。非法整体 → null(回退旧视图)。
// ============================================================

const GROUP_SOURCE_KINDS = new Set([
  'activeEntity', 'entityByField', 'entityHistory', 'assetField', 'stageOutput', 'singleton',
])
const DISPLAY_KINDS = new Set([
  'preview', 'previewKv', 'fields', 'kv', 'assetPreviews', 'thumbnailList', 'empty', 'form',
])

function parseGroupSource(raw: unknown): PipelineGroupSource | null {
  if (!isRecord(raw)) return null
  const kind = asString(raw.kind)
  if (!GROUP_SOURCE_KINDS.has(kind)) return null
  switch (kind) {
    case 'activeEntity':
      return { kind: 'activeEntity' }
    case 'entityByField':
      if (!asString(raw.entityType) || !asString(raw.field)) return null
      return { kind: 'entityByField', entityType: asString(raw.entityType), field: asString(raw.field) }
    case 'entityHistory':
      if (!asString(raw.keyField)) return null
      return {
        kind: 'entityHistory',
        keyField: asString(raw.keyField),
        entityType: asString(raw.entityType) || undefined,
        runIdField: asString(raw.runIdField) || undefined,
      }
    case 'assetField':
      if (!asString(raw.field)) return null
      return { kind: 'assetField', field: asString(raw.field) }
    case 'stageOutput':
      if (!asString(raw.stageId) || !asString(raw.field)) return null
      return { kind: 'stageOutput', stageId: asString(raw.stageId), field: asString(raw.field) }
    case 'singleton':
      if (!asString(raw.entityType)) return null
      return { kind: 'singleton', entityType: asString(raw.entityType) }
    default:
      return null
  }
}

function parseDisplaySpec(raw: unknown): EntityDisplaySpec | null {
  if (!isRecord(raw)) return null
  const kind = asString(raw.kind)
  if (!DISPLAY_KINDS.has(kind)) return null
  switch (kind) {
    case 'preview':
      if (!asString(raw.field)) return null
      return { kind: 'preview', field: asString(raw.field), fallbackField: asString(raw.fallbackField) || undefined }
    case 'previewKv':
      if (!asString(raw.previewField)) return null
      return { kind: 'previewKv', previewField: asString(raw.previewField), fields: asStringArray(raw.fields) }
    case 'fields':
      return { kind: 'fields', fields: asStringArray(raw.fields) }
    case 'kv':
      return { kind: 'kv', fields: asStringArray(raw.fields) }
    case 'assetPreviews':
      return { kind: 'assetPreviews', fields: asStringArray(raw.fields) }
    case 'thumbnailList':
      if (!asString(raw.thumbField)) return null
      return {
        kind: 'thumbnailList',
        thumbField: asString(raw.thumbField),
        labelField: asString(raw.labelField) || undefined,
        metaFields: asStringArray(raw.metaFields),
      }
    case 'empty':
      return { kind: 'empty', textKey: asString(raw.textKey) || undefined }
    case 'form':
      return { kind: 'form', fields: asStringArray(raw.fields) }
    default:
      return null
  }
}

function parseGroup(raw: unknown): PipelineObjectGroupSpec | null {
  if (!isRecord(raw)) return null
  const id = asString(raw.id)
  if (!id) return null
  const source = parseGroupSource(raw.source)
  const display = parseDisplaySpec(raw.display)
  if (!source || !display) return null
  return { id, labelKey: asString(raw.labelKey) || undefined, label: asString(raw.label) || undefined, source, display }
}

/** ChainArgSpec 闭集解析;返回包裹对象以区分「合法 literal null」与「非法(返回 null)」。 */
function parseChainArg(raw: unknown): { arg: ChainArgSpec } | null {
  if (raw === null || typeof raw === 'string' || typeof raw === 'number' || typeof raw === 'boolean') {
    return { arg: raw as ChainArgSpec }
  }
  if (isRecord(raw) && typeof raw.source === 'string') {
    if (raw.source === 'activeEntity' && typeof raw.field === 'string') {
      return { arg: { source: 'activeEntity', field: raw.field } }
    }
    if (raw.source === 'groupEntity' && typeof raw.groupId === 'string' && typeof raw.field === 'string') {
      return { arg: { source: 'groupEntity', groupId: raw.groupId, field: raw.field } }
    }
    if (raw.source === 'literal' && 'value' in raw) {
      return { arg: { source: 'literal', value: raw.value } }
    }
  }
  return null
}

function parseStep(raw: unknown): PipelineCommandStepSpec | null {
  if (!isRecord(raw)) return null
  const id = asString(raw.id)
  const from = asString(raw.from)
  const to = asString(raw.to)
  const commandId = asString(raw.commandId)
  if (!id || !from || !to || !commandId) return null
  const args: Record<string, ChainArgSpec> = {}
  if (isRecord(raw.args)) {
    for (const [k, v] of Object.entries(raw.args)) {
      const parsed = parseChainArg(v)
      if (parsed) args[k] = parsed.arg
    }
  }
  return {
    id,
    labelKey: asString(raw.labelKey) || undefined,
    label: asString(raw.label) || id,
    from,
    to,
    commandId,
    stageIds: Array.isArray(raw.stageIds) ? asStringArray(raw.stageIds) : undefined,
    args: Object.keys(args).length > 0 ? args : undefined,
    defaultOpen: raw.defaultOpen === true,
  }
}

function parseDetails(raw: unknown): Required<PipelineDetailsSpec> {
  const d = isRecord(raw) ? raw : {}
  return {
    defaultOpen: d.defaultOpen === true,
    showStartConsole: d.showStartConsole !== false,
    showNotebook: d.showNotebook !== false,
    showMatrix: d.showMatrix !== false,
  }
}

/** 解析 view_state.chain。非 record / 无合法 group → null(调用方回退 display.cells / notebook)。 */
export function parsePipelineChainConfig(raw: unknown): ParsedPipelineChain | null {
  if (!isRecord(raw)) return null
  if (!Array.isArray(raw.groups) || !Array.isArray(raw.steps)) return null
  const groups = raw.groups.map(parseGroup).filter((g): g is PipelineObjectGroupSpec => g !== null)
  if (groups.length === 0) return null
  const groupIds = new Set(groups.map((g) => g.id))
  // 只保留两端都指向已解析对象组的 step(避免悬空连线)。
  const steps = raw.steps
    .map(parseStep)
    .filter((s): s is PipelineCommandStepSpec => s !== null && groupIds.has(s.from) && groupIds.has(s.to))
  return { groups, steps, details: parseDetails(raw.details) }
}

// ============================================================
// 解算(resolve):对象组 source → 实体/快照。
// ============================================================

export type ResolvedPipelineObjectGroup = {
  spec: PipelineObjectGroupSpec
  /** 主体实体(activeEntity / entityByField / history 首个);stageOutput 时为 null。 */
  entity: EntityDict | null
  /** 列表(history / thumbnailList);其余为单元素或空。 */
  entities: EntityDict[]
  /** stageOutput source 的 output 快照;其余为 null。 */
  snapshot: Record<string, unknown> | null
  /** 解析不到主体(置灰/empty 态)。 */
  missing: boolean
}

export type ResolveGroupContext = {
  activeEntity: EntityDict | null
  entitiesByType: Record<string, EntityDict[]>
  /** 主类型实体(entitiesByType 缺该类型时的兜底)。 */
  entities: EntityDict[]
  steps: PipelineStepState[]
}

export function resolvePipelineObjectGroup(
  spec: PipelineObjectGroupSpec,
  ctx: ResolveGroupContext,
): ResolvedPipelineObjectGroup {
  const { activeEntity, entitiesByType, entities, steps } = ctx
  const src = spec.source
  switch (src.kind) {
    case 'activeEntity':
      return { spec, entity: activeEntity, entities: activeEntity ? [activeEntity] : [], snapshot: null, missing: !activeEntity }
    case 'entityByField': {
      const pool = entitiesByType[src.entityType] ?? []
      const rawId = asString(readChainField(activeEntity, src.field))
      let entity = rawId ? pool.find((e) => e.id === rawId && !e.deleted) ?? null : null
      // 首跑 fallback(plan §10.2):active 为空 / 取不到 id 时,若该类型只剩一个实体,用它。
      if (!entity) {
        const live = pool.filter((e) => !e.deleted)
        if (live.length === 1) entity = live[0]
      }
      return { spec, entity, entities: entity ? [entity] : [], snapshot: null, missing: !entity }
    }
    case 'entityHistory': {
      const pool = src.entityType ? (entitiesByType[src.entityType] ?? []) : entities
      const key = asString(readChainField(activeEntity, src.keyField))
      const list = key
        ? pool.filter((e) => !e.deleted && asString(readChainField(e, src.keyField)) === key)
        : []
      const entity = activeEntity && list.some((e) => e.id === activeEntity.id)
        ? activeEntity
        : (list[0] ?? null)
      return { spec, entity, entities: list, snapshot: null, missing: list.length === 0 }
    }
    case 'assetField':
      // 资产挂在 active entity 的字段上;主体仍是 active entity,display 读该字段的 asset ref。
      return { spec, entity: activeEntity, entities: activeEntity ? [activeEntity] : [], snapshot: null, missing: !activeEntity }
    case 'stageOutput': {
      const step = steps.find((s) => s.stage_id === src.stageId)
      const snapshot = step?.output_snapshot ?? null
      return { spec, entity: null, entities: [], snapshot, missing: !snapshot }
    }
    case 'singleton': {
      // 文档级唯一实体:取该类型首个未删实体(与选择无关)。缺则置灰(missing)。
      const entity = (entitiesByType[src.entityType] ?? []).find((e) => !e.deleted) ?? null
      return { spec, entity, entities: entity ? [entity] : [], snapshot: null, missing: !entity }
    }
    default:
      return { spec, entity: null, entities: [], snapshot: null, missing: true }
  }
}

/** 统一字段读取:stageOutput 组读快照,其余读实体(根字段/ data)。供渲染层 display 用。 */
export function readGroupField(resolved: ResolvedPipelineObjectGroup, field: string): unknown {
  if (resolved.snapshot) return resolved.snapshot[field]
  return readChainField(resolved.entity, field)
}

// ============================================================
// 解算(resolve):command 参数闭集 → payload + 缺位原因。
// ============================================================

export type ResolveArgContext = {
  activeEntity: EntityDict | null
  /** groupId → 该对象组解算出的主体实体(无则 null)。 */
  groupEntities: Record<string, EntityDict | null>
}

export function resolveChainArg(
  spec: ChainArgSpec,
  ctx: ResolveArgContext,
): { value?: unknown; blockedReason?: string } {
  if (spec === null || typeof spec === 'string' || typeof spec === 'number' || typeof spec === 'boolean') {
    return { value: spec }
  }
  if (spec.source === 'literal') return { value: spec.value }
  if (spec.source === 'activeEntity') {
    if (!ctx.activeEntity) return { blockedReason: `activeEntity required (${spec.field})` }
    return { value: readChainField(ctx.activeEntity, spec.field) }
  }
  if (spec.source === 'groupEntity') {
    const ent = ctx.groupEntities[spec.groupId] ?? null
    if (!ent) return { blockedReason: `group "${spec.groupId}" has no entity` }
    return { value: readChainField(ent, spec.field) }
  }
  return { blockedReason: 'invalid arg' }
}

/** 解算整步参数:payload(可派发的 args)+ blockedReasons(非空 → command 置灰并示因)。 */
export function resolveStepArgs(
  step: PipelineCommandStepSpec,
  ctx: ResolveArgContext,
): { payload: Record<string, unknown>; blockedReasons: string[] } {
  const payload: Record<string, unknown> = {}
  const blockedReasons: string[] = []
  for (const [arg, spec] of Object.entries(step.args ?? {})) {
    const r = resolveChainArg(spec, ctx)
    if (r.blockedReason) blockedReasons.push(r.blockedReason)
    else payload[arg] = r.value
  }
  // 默认注入 activeEntity id(step.args 未显式提供 id 时)——与 StartConsole buildStartDraft 对齐:
  // 链步作用于管线当前实体,需要 id 的 stage 处理器据此解析目标;显式 step.args.id 仍优先。
  if (payload.id === undefined) {
    const aid = readChainField(ctx.activeEntity, 'id')
    if (aid !== undefined && aid !== null && aid !== '') payload.id = aid
  }
  return { payload, blockedReasons }
}

// ============================================================
// 合并多 stage 的运行态(phase / progress / error)。
// ============================================================

/** 多 step 合并相位(单一真相源,与 entity 级 phaseForEntity 共用次序):
 *  running > interrupted > error > stale > 全 done > 首个 pending。 */
export function phaseFromSteps(owned: PipelineStepState[]): string {
  if (owned.some((s) => s.phase === 'running')) return 'running'
  if (owned.some((s) => s.phase === 'interrupted')) return 'interrupted'
  if (owned.some((s) => s.phase === 'error')) return 'error'
  if (owned.some((s) => s.phase === 'stale')) return 'stale'
  if (owned.length && owned.every((s) => s.phase === 'done')) return 'done'
  return owned[0]?.phase ?? 'pending'
}

/** step_states 里属于这条 command 的 step:优先 stageIds(stage_id 闭集),回退 command_id。 */
function matchStageSteps(steps: PipelineStepState[], stageIds?: string[], commandId?: string): PipelineStepState[] {
  if (stageIds && stageIds.length > 0) return steps.filter((s) => stageIds.includes(s.stage_id))
  if (commandId) return steps.filter((s) => s.command_id === commandId)
  return []
}

/** 合并相位;尚无任何对应 step(从未跑)→ 'idle'(command 显示 Run)。 */
export function phaseForMergedStages(steps: PipelineStepState[], stageIds?: string[], commandId?: string): string {
  const owned = matchStageSteps(steps, stageIds, commandId)
  if (owned.length === 0) return 'idle'
  return phaseFromSteps(owned)
}

/** 合并进度 0..1:done=1、其余 stage 只消费自身真实 progress,取均值。
 *  任一未完成 stage 没有有限数值时,整条链保持 indeterminate(null),不得把 pending/error 等未知工作伪装成 0。
 *  无 step 同样返回 null;全 done 则由完成事实得到 1。 */
export function progressForMergedStages(steps: PipelineStepState[], stageIds?: string[], commandId?: string): number | null {
  const owned = matchStageSteps(steps, stageIds, commandId)
  if (owned.length === 0) return null
  let sum = 0
  for (const s of owned) {
    if (s.phase === 'done') sum += 1
    else {
      if (typeof s.progress !== 'number' || !Number.isFinite(s.progress)) return null
      sum += Math.max(0, Math.min(1, s.progress))
    }
  }
  return Math.max(0, Math.min(1, sum / owned.length))
}

/** 合并错误:第一个 error 相位且带 error 的 step → 结构化 RunError;无则 null。 */
export function errorForMergedStages(steps: PipelineStepState[], stageIds?: string[], commandId?: string): RunError | null {
  const owned = matchStageSteps(steps, stageIds, commandId)
  const errStep = owned.find((s) => s.phase === 'error' && s.error)
  if (!errStep || !errStep.error) return null
  return {
    code: errStep.error.code || 'error',
    message: errStep.error.message || '',
    trace_id: errStep.error.trace_id,
  }
}
