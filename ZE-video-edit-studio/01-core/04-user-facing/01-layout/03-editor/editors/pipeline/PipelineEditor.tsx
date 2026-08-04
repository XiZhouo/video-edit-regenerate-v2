/**
 * PipelineEditor — build v3 pipeline notebook editor.
 *
 * Standard product mode is a fixed pipeline declared by slot view_state. A run
 * is rendered as a notebook: one command/stage per cell, with input/output
 * snapshots, logs, errors, artifacts, and lifecycle controls.
 */

import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { EditorFrame } from '../../EditorFrame'
import { HeaderZone } from '../../zones/HeaderZone'
import { ControlZone } from '../../zones/ControlZone'
import { FunctionZone } from '../../zones/FunctionZone'
import { renderToolbarAction } from '../../zones/renderToolbarAction'
import { ParamPanel } from '../../primitives/ParamPanel'
import { FormZone } from '../../zones/FormZone'
import { ThumbnailGrid, type ThumbItem } from '../../primitives/ThumbnailGrid'
import { RUN_STATUS_ALIAS, isKnownStatus } from '../../lib/taskStages'
import {
  buildStartDraft, missingRequiredParams, paramProperties, parseRequiresRuntime,
  requiredParamNames, runIsActive, type RequiresRuntime,
} from '../../lib/pipelineConsole'
import {
  parsePipelineChainConfig, phaseFromSteps, phaseForMergedStages, progressForMergedStages,
  errorForMergedStages, resolvePipelineObjectGroup, readGroupField, resolveStepArgs,
  type ParsedPipelineChain, type PipelineCommandStepSpec, type ResolvedPipelineObjectGroup,
  type ResolveGroupContext,
} from '../../lib/pipelineChain'
import { notebookToJson, notebookToMarkdown } from '../../lib/pipelineNotebookExport'
import { useScrollAffordance } from '../../lib/useScrollAffordance'
import type { ToolbarAction } from '../../zones/types'
import type { AssetRefDict, AssetUrlFn, DispatchFn, EntityDict, EntityTypeSchema, StatusLookup } from '../../types'
import type {
  CommandSpecBrief, PipelineLogEvent, PipelinePort, PipelineRun, PipelineStaticMeta, PipelineStepState,
} from '../../../02-footer'

export type PipelineEntityUi = {
  labelField?: string
  thumbField?: string
  resultField?: string
  itemIdField?: string
  statusAnimation?: 'none' | 'progress-outline' | 'soft-veil' | 'scanline' | 'thumbnail-pulse' | string
  summaryFields?: string[]
}

/** 管线显示开关(声明式):控制每步 cell 显示哪些内容,避免"所有步骤所有内容一一显示"。
 *  缺省全 true(向后兼容);ports 白名单空=全部。运行时「全量显示」开关可覆盖全部为显示。 */
/** 合并格(Jupyter 式取舍):把整条管线**合一**成几张「命令格」,每格只露输入图→输出图+控制钮。
 *  声明 cells 即默认走合并视图(干净操作面板);逐步骤/日志/导出等细节收进「全量显示」。
 *  输入/输出图取**当前结果实体(候选)**上的字段(版本下拉切换历次)。 */
export type PipelineCellSpec = {
  label?: string            // 合并后这一格的名字(语言中立回退;可译走 labelKey)
  labelKey?: string         // 格名可译 chrome key(走 t());优先于 label
  inputField?: string       // 输入图取候选哪个字段(如 guideRef = 抠图后引导图)
  inputFallback?: string    // inputField 缺则回退(如 sourceRef)
  inputLabel?: string       // 输入区小标题(语言中立回退)
  inputLabelKey?: string    // 输入区小标题可译 key(走 t());优先于 inputLabel
  outputField?: string      // 输出图字段(如 resultRef)
  outputLabel?: string      // 输出区小标题(语言中立回退)
  outputLabelKey?: string   // 输出区小标题可译 key(走 t());优先于 outputLabel
  runCommand?: string       // 运行/重跑派发的命令(如 object.finalize_inline)
  runTargetType?: string    // 运行目标实体类型(无候选时取该类型唯一实体当 target)
  runTargetField?: string   // 候选→运行目标 id 的字段(如 sourceObjectId);重跑用「选中候选」的它
}

export type PipelineDisplayConfig = {
  showInputs?: boolean      // 显示输入快照(默认 true)
  showOutputs?: boolean     // 显示输出快照(默认 true)
  showLogs?: boolean        // 显示日志(默认 true)
  showOperations?: boolean  // 显示 operation chips(默认 true)
  showExport?: boolean      // 显示导出 notebook(JSON/MD)控制行(默认 true);纯展示工具可关
  ports?: string[]          // 输入/输出快照只显示这些 port id(空=全部)
  cells?: Required<PipelineCellSpec>[]  // 声明 → 默认渲合并格(输入→输出+控制);归一化后形态(parseCell)
}

export type PipelineEditorViewState = {
  pipelineId?: string
  startCommandId?: string
  allowRunSwitch?: boolean
  runIdField?: string
  itemIdField?: string
  pipelineIdField?: string
  resultField?: string
  /** 每元素运行历史的分组键:同一 historyKeyField 值的 entity = 同一逻辑元素的多次结果
   *  (如 finalizer 'sourceObjectId':同一源物体多次 finalize)。未声明 → 不渲历史列表(向后兼容)。 */
  historyKeyField?: string
  /** 显示开关:管线编辑器**支持**显示一切,但 mod 据工具用途声明默认显示什么(不是所有步骤所有内容
   *  都一一显示)。mod 写部分字段即可(parseDisplay 归一化为全字段);缺省全显示(向后兼容);
   *  用户点「全量显示」可一键覆盖看全部。(类型为 Required = parsePipelineViewState 归一化后的形态。) */
  display?: Required<PipelineDisplayConfig>
  entityUi?: PipelineEntityUi
  /** 操作台(StartConsole)固定参数:并进 start 命令 args(P-4 闭集,声明式)。 */
  startArgs?: Record<string, unknown>
  /** 操作台参数 ← 选中实体字段注入:{argName: entityField}(id/name/type/data 字段)。 */
  argsFromSelection?: Record<string, string>
  /** 链路主视图(对象组 → command → 对象组)的声明式配置(已解析)。
   *  parsePipelineChainConfig 解析 view_state.chain;非法 / 缺省 → null(回退 display.cells / notebook)。 */
  chain?: ParsedPipelineChain | null
}

export type PipelineEditorProps = {
  /** 编辑器名(横栏左标题;mod 经 slot.name 声明)。缺省回退 pipelineId/命令名。 */
  editorName?: string
  entityType: string
  entities: EntityDict[]
  selectedId?: string | null
  selected?: EntityDict | null
  viewState?: Record<string, unknown>
  pipelineRuns?: PipelineRun[]
  commandSpecs?: CommandSpecBrief[]
  pipelineSpecs?: PipelineStaticMeta[]
  entitiesByType?: Record<string, EntityDict[]>
  statusOf?: StatusLookup
  assetUrl?: AssetUrlFn
  /** 实体类型 schema 表(供 form 节点据声明字段渲可编辑控件;经 SlotContext.schemas 透传)。 */
  schemas?: Record<string, EntityTypeSchema | undefined>
  /** 资产上传缝(供 form 节点的 asset 字段上传;缺省 → asset 字段降级只读预览)。 */
  uploadAsset?: (bytes: Uint8Array, mime: string, ext?: string) => Promise<AssetRefDict>
  dispatch?: DispatchFn
  onSelect?: (id: string | null) => void
}

type TFn = ReturnType<typeof useT>

function isRecord(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === 'object' && !Array.isArray(v)
}

function asString(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

function asBool(v: unknown): boolean {
  return v === true
}

function asStringArray(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
}

function readEntityField(entity: EntityDict | null | undefined, field?: string): unknown {
  if (!entity || !field) return undefined
  if (field === 'id') return entity.id
  if (field === 'name') return entity.name
  if (field === 'type') return entity.type
  if (field === 'version') return entity.version
  return entity.data?.[field]
}

function fmtTime(ts?: number | null): string {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleTimeString()
}

function fmtDuration(start?: number, end?: number): string {
  if (!start || !end || end < start) return ''
  const s = end - start
  return `${s.toFixed(s < 10 ? 1 : 0)}s`
}

function pct(v?: number | null): string {
  if (v === undefined || v === null) return ''
  return `${Math.round(Math.max(0, Math.min(1, v)) * 100)}%`
}

// 字段键 → 可读 label(camelCase/snake/kebab → 词首大写)。pipeline 摘要不再"字段名当 label"。
function humanizeField(field: string): string {
  return field
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_-]+/g, ' ')
    .replace(/^./, (c) => c.toUpperCase())
    .trim()
}

// 摘要值格式化:覆盖率/比率类 0~1 数 → 百分比;*At/*Time/*Date 的 epoch(容忍秒/毫秒)→ 本地时间;
// 其余原样。pipeline 摘要不再堆裸值。
function formatSummaryValue(field: string, value: unknown): string {
  if (typeof value === 'number') {
    if (/coverage|ratio|percent/i.test(field) && value >= 0 && value <= 1) return pct(value)
    if (/(at|time|date)$/i.test(field) && value > 1e8) {
      const sec = value > 1e12 ? value / 1000 : value
      return new Date(sec * 1000).toLocaleString()
    }
    return String(value)
  }
  return String(value ?? '')
}

function stepKey(s: PipelineStepState): string {
  return `${s.item_id}::${s.stage_id}`
}

function parseEntityUi(raw: unknown, fallbackItemIdField: string, fallbackResultField: string): PipelineEntityUi {
  const rec = isRecord(raw) ? raw : {}
  return {
    labelField: asString(rec.labelField) || 'name',
    thumbField: asString(rec.thumbField) || fallbackResultField || undefined,
    resultField: asString(rec.resultField) || fallbackResultField || undefined,
    itemIdField: asString(rec.itemIdField) || fallbackItemIdField,
    statusAnimation: asString(rec.statusAnimation) || 'progress-outline',
    summaryFields: asStringArray(rec.summaryFields),
  }
}

function asStringMap(v: unknown): Record<string, string> {
  if (!isRecord(v)) return {}
  const out: Record<string, string> = {}
  for (const [k, x] of Object.entries(v)) if (typeof x === 'string') out[k] = x
  return out
}

// 显示开关归一化:缺省全显示(show* !== false),collapseSteps 缺省 false,ports 缺省空(=全部)。
function parseCell(v: unknown): Required<PipelineCellSpec> {
  const c = isRecord(v) ? v : {}
  return {
    label: asString(c.label),
    labelKey: asString(c.labelKey),
    inputField: asString(c.inputField),
    inputFallback: asString(c.inputFallback),
    inputLabel: asString(c.inputLabel),
    inputLabelKey: asString(c.inputLabelKey),
    outputField: asString(c.outputField),
    outputLabel: asString(c.outputLabel),
    outputLabelKey: asString(c.outputLabelKey),
    runCommand: asString(c.runCommand),
    runTargetType: asString(c.runTargetType),
    runTargetField: asString(c.runTargetField),
  }
}

function parseDisplay(v: unknown): Required<PipelineDisplayConfig> {
  const d = isRecord(v) ? v : {}
  return {
    showInputs: d.showInputs !== false,
    showOutputs: d.showOutputs !== false,
    showLogs: d.showLogs !== false,
    showOperations: d.showOperations !== false,
    showExport: d.showExport !== false,
    ports: asStringArray(d.ports),
    cells: Array.isArray(d.cells) ? d.cells.map(parseCell) : [],
  }
}

/** 该 display 配置是否隐藏了任何东西(决定要不要渲「全量显示」开关)。 */
function displayHidesSomething(d: Required<PipelineDisplayConfig>): boolean {
  return !d.showInputs || !d.showOutputs || !d.showLogs || !d.showOperations || d.ports.length > 0 || d.cells.length > 0
}

export function parsePipelineViewState(viewState?: Record<string, unknown>): Required<PipelineEditorViewState> {
  const view = viewState ?? {}
  const runIdField = asString(view.runIdField) || 'runId'
  const itemIdField = asString(view.itemIdField) || 'itemId'
  const pipelineIdField = asString(view.pipelineIdField) || 'pipelineId'
  const resultField = asString(view.resultField) || ''
  return {
    pipelineId: asString(view.pipelineId),
    startCommandId: asString(view.startCommandId),
    allowRunSwitch: asBool(view.allowRunSwitch),
    runIdField,
    itemIdField,
    pipelineIdField,
    resultField,
    historyKeyField: asString(view.historyKeyField),
    display: parseDisplay(view.display),
    entityUi: parseEntityUi(view.entityUi, itemIdField, resultField),
    startArgs: isRecord(view.startArgs) ? view.startArgs : {},
    argsFromSelection: asStringMap(view.argsFromSelection),
    // 非法 chain 不 throw、回退 null(旧 display.cells / notebook 视图照常工作)。
    chain: parsePipelineChainConfig(view.chain),
  }
}

function latestRun(runs: PipelineRun[]): PipelineRun | null {
  if (runs.length === 0) return null
  return runs.slice().sort((a, b) => (b.started_at || 0) - (a.started_at || 0))[0]
}

function runHasEntity(run: PipelineRun, entity: EntityDict | null, itemIdField: string): boolean {
  if (!entity) return false
  const entityItemId = asString(readEntityField(entity, itemIdField)) || entity.id
  return (run.step_states ?? []).some((s) =>
    s.entity_id === entity.id || s.item_id === entityItemId || s.item_id === entity.id)
}

export function selectPipelineRun(
  runs: PipelineRun[],
  entity: EntityDict | null,
  view: Required<PipelineEditorViewState>,
  manualRunId = '',
): PipelineRun | null {
  if (manualRunId && view.allowRunSwitch) {
    return runs.find((r) => r.id === manualRunId) ?? null
  }
  // 每元素运行历史:选中某候选(historyKeyField 声明)→ 跟它自己的 run,优先于固定管线"取最新"
  // 的短路 —— 这样点历史卡(=选中那次结果的候选)能真切到那次 run,而非永远显示最新。
  if (view.historyKeyField) {
    const ownRunId = asString(readEntityField(entity, view.runIdField))
    if (ownRunId) {
      const found = runs.find((r) => r.id === ownRunId)
      if (found) return found
    }
  }
  if (view.pipelineId || view.startCommandId) {
    const found = latestRun(runs.filter((r) =>
      (view.pipelineId && r.pipeline_id === view.pipelineId) ||
      (view.startCommandId && r.start_command_id === view.startCommandId)))
    if (found) return found
  }
  const entityRunId = asString(readEntityField(entity, view.runIdField))
  if (entityRunId) {
    const found = runs.find((r) => r.id === entityRunId)
    if (found) return found
  }
  const entityPipelineId = asString(readEntityField(entity, view.pipelineIdField))
  if (entityPipelineId) {
    const found = latestRun(runs.filter((r) => r.pipeline_id === entityPipelineId))
    if (found) return found
  }
  const entityRun = latestRun(runs.filter((r) => runHasEntity(r, entity, view.itemIdField)))
  if (entityRun) return entityRun
  return view.allowRunSwitch || (!view.pipelineId && !view.startCommandId) ? latestRun(runs) : null
}

/** 每元素运行历史:与 activeEntity 同 historyKeyField 值的全部 entity(候选)= 同一逻辑元素的
 *  多次结果(如同一 sourceObjectId 的多次 finalize)。纯过滤、可单测;排序由调用方按 run started_at 定。
 *  historyKeyField 未声明 / activeEntity 无键 → 空(不渲历史,向后兼容)。软删的不计入。 */
export function elementCandidates(
  entities: EntityDict[],
  activeEntity: EntityDict | null,
  historyKeyField: string,
): EntityDict[] {
  if (!historyKeyField || !activeEntity) return []
  const key = asString(readEntityField(activeEntity, historyKeyField))
  if (!key) return []
  return entities.filter((e) => !e.deleted && asString(readEntityField(e, historyKeyField)) === key)
}

function commandMap(commandSpecs: CommandSpecBrief[]): Map<string, CommandSpecBrief> {
  const m = new Map<string, CommandSpecBrief>()
  for (const c of commandSpecs) m.set(c.id, c)
  return m
}

function stageMetaFor(
  pipelineSpecs: PipelineStaticMeta[],
  run: PipelineRun | null,
  step: PipelineStepState,
): NonNullable<PipelineStaticMeta['stages']>[number] | undefined {
  const spec = pipelineSpecs.find((p) => p.id === run?.pipeline_id)
  return spec?.stages?.find((s) => s.id === step.stage_id)
}

function portsForStep(
  direction: 'input' | 'output',
  step: PipelineStepState,
  command: CommandSpecBrief | undefined,
  stageMeta?: NonNullable<PipelineStaticMeta['stages']>[number],
): PipelinePort[] {
  const fromStage = direction === 'input' ? stageMeta?.input_ports : stageMeta?.output_ports
  if (fromStage?.length) return fromStage
  const fromCommand = command?.ui_ports?.[direction === 'input' ? 'inputs' : 'outputs']
  if (Array.isArray(fromCommand) && fromCommand.length) return fromCommand
  if (direction === 'output' && command?.outputs_schema) {
    return Object.entries(command.outputs_schema).map(([id, schema]) => {
      const rec = isRecord(schema) ? schema : {}
      return {
        id,
        label: asString(rec.label) || id,
        kind: asString(rec.type) || 'json',
        source: 'stage_output',
        field: id,
        preview: asString(rec.preview) || undefined,
      }
    })
  }
  const snap = direction === 'input' ? step.input_snapshot : step.output_snapshot
  return Object.keys(snap ?? {}).map((id) => ({ id, label: id, kind: 'json', source: direction === 'input' ? 'args' : 'stage_output' }))
}

export function snapshotEntries(snapshot: Record<string, unknown> | undefined, ports: PipelinePort[], strict = false): Array<{ port: PipelinePort; value: unknown }> {
  const snap = snapshot ?? {}
  const seen = new Set<string>()
  const out: Array<{ port: PipelinePort; value: unknown }> = []
  for (const p of ports) {
    const id = p.id || p.field || p.path || ''
    if (!id) continue
    seen.add(id)
    out.push({ port: { ...p, id }, value: snap[id] })
  }
  // strict(显示开关 ports 白名单生效时):只显示声明的 port,不追加未声明的快照键 —— 否则白名单
  // 过滤会被未声明键泄漏架空。非 strict 维持原行为(声明优先 + 追加 fallback 键)。
  if (!strict) {
    for (const [id, value] of Object.entries(snap)) {
      if (seen.has(id)) continue
      out.push({ port: { id, label: id, kind: inferPortKind(value), source: 'stage_output' }, value })
    }
  }
  return out
}

function inferPortKind(value: unknown): string {
  if (valueToAsset(value)) return 'asset'
  if (typeof value === 'number') return 'metric'
  if (typeof value === 'boolean') return 'boolean'
  if (typeof value === 'string') return 'text'
  return 'json'
}

function valueToAsset(value: unknown): AssetRefDict | null {
  if (!isRecord(value)) return null
  const ref = value.ref
  if (isRecord(ref) && typeof ref.sha256 === 'string') return ref as AssetRefDict
  if (typeof value.sha256 === 'string') return value as AssetRefDict
  return null
}

function valueToUrl(value: unknown, assetUrl?: AssetUrlFn): string {
  if (isRecord(value) && typeof value.url === 'string') return value.url
  const asset = valueToAsset(value)
  return asset && assetUrl ? assetUrl(asset) : ''
}

async function copyText(text: string): Promise<void> {
  await navigator.clipboard?.writeText(text)
}

async function copyImage(url: string): Promise<void> {
  const nav = navigator as Navigator & {
    clipboard?: Clipboard & { write?: (items: ClipboardItem[]) => Promise<void> }
  }
  if (!nav.clipboard?.write || typeof ClipboardItem === 'undefined') {
    await copyText(url)
    return
  }
  const blob = await fetch(url).then((r) => r.blob())
  await nav.clipboard.write([new ClipboardItem({ [blob.type || 'image/png']: blob })])
}

function downloadUrl(url: string, name: string): void {
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
}

function downloadText(text: string, name: string, mime: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: mime }))
  downloadUrl(url, name)
  URL.revokeObjectURL(url)
}

/** RunPhase 显示词过单收口点(taskStages.RUN_STATUS_ALIAS → task.status.* i18n;
 *  不在本文件自造第二张映射表)。data-phase 属性仍挂原始 RunPhase 词(CSS 契约键不动)。 */
function phaseLabel(phase: string, t: TFn): string {
  const k = RUN_STATUS_ALIAS[phase] ?? phase
  if (isKnownStatus(k)) return t(`task.status.${k}`)
  if (k === 'skipped' || k === 'interrupted' || k === 'stale') return t(`task.status.${k}`)
  return k
}

// 相位 → .ze-badge / .ze-progress 语义变体(L2 原子据 data-variant 上语义色,可主题化)。
// pending 不给变体 = 中性底(原子缺省),done→success / running→info / error→danger /
// interrupted·stale→warn。仅此一处映射,badge 与 progress 复用。
function phaseVariant(phase: string): string | undefined {
  switch (phase) {
    case 'done': return 'success'
    case 'running': return 'info'
    case 'error': return 'danger'
    case 'interrupted':
    case 'stale': return 'warn'
    default: return undefined
  }
}

function entityForValue(
  value: unknown,
  port: PipelinePort,
  entitiesByType: Record<string, EntityDict[]>,
): EntityDict | null {
  const type = port.entityType || (isRecord(value) ? asString(value.entity_type) || asString(value.type) : '')
  const id = isRecord(value) ? asString(value.entity_id) || asString(value.id) : asString(value)
  if (!type || !id) return null
  return (entitiesByType[type] ?? []).find((e) => e.id === id) ?? null
}

function PortValueRenderer({
  entry, assetUrl, entitiesByType, t,
}: {
  entry: { port: PipelinePort; value: unknown }
  assetUrl?: AssetUrlFn
  entitiesByType: Record<string, EntityDict[]>
  t: TFn
}): ReactNode {
  const { port, value } = entry
  const label = port.label || port.id
  const kind = port.kind || inferPortKind(value)
  const jsonRef = useScrollAffordance('y')
  if (port.secret) {
    return (
      <div className="ze-pipeline__kv">
        <span>{label}</span>
        <strong>{value ? t('editor.pipelineConfigured') : t('editor.pipelineNotConfigured')}</strong>
      </div>
    )
  }
  const url = valueToUrl(value, assetUrl)
  if (url && ['image', 'mask', 'asset'].includes(kind)) {
    return (
      <div className="ze-pipeline__port" data-kind={kind}>
        <div className="ze-pipeline__port-head">
          <span>{label}</span>
          <span className="ze-pipeline__port-actions">
            <button type="button" className="ze-button" data-size="sm" data-variant="ghost" onClick={() => void copyImage(url)}>{t('editor.copyImage')}</button>
            <button type="button" className="ze-button" data-size="sm" data-variant="ghost" onClick={() => downloadUrl(url, `${port.id || 'asset'}.png`)}>{t('editor.download')}</button>
          </span>
        </div>
        <img className="ze-pipeline__image" src={url} alt={label} draggable={false} />
      </div>
    )
  }
  if (kind === 'entity') {
    const ent = entityForValue(value, port, entitiesByType)
    return (
      <div className="ze-pipeline__kv">
        <span>{label}</span>
        <strong>{ent?.name || (isRecord(value) ? asString(value.entity_id) || asString(value.id) : String(value ?? ''))}</strong>
      </div>
    )
  }
  if (kind === 'artifact' || (isRecord(value) && value.kind === 'artifact')) {
    const path = isRecord(value) ? asString(value.path) : String(value ?? '')
    const present = isRecord(value) ? value.present === true : false
    return (
      <div className="ze-pipeline__kv" data-present={present ? 'true' : 'false'}>
        <span>{label}</span>
        <strong>{path || t('editor.pipelineNoArtifact')}</strong>
      </div>
    )
  }
  if (kind === 'metric' || typeof value === 'number' || typeof value === 'boolean') {
    return (
      <div className="ze-pipeline__kv">
        <span>{label}</span>
        <strong>{String(value ?? '')}</strong>
      </div>
    )
  }
  if (kind === 'text' || typeof value === 'string') {
    return (
      <div className="ze-pipeline__kv">
        <span>{label}</span>
        <strong>{String(value ?? '')}</strong>
      </div>
    )
  }
  return (
    <div className="ze-pipeline__port" data-kind={kind}>
      <div className="ze-pipeline__port-head">
        <span>{label}</span>
        <button
          type="button" className="ze-button" data-size="sm" data-variant="ghost"
          onClick={() => void copyText(JSON.stringify(value, null, 2))}
        >{t('editor.pipelineCopyJson')}</button>
      </div>
      <pre ref={jsonRef} className="ze-pipeline__json ze-scroll-fade-y">{JSON.stringify(value ?? null, null, 2)}</pre>
    </div>
  )
}

function SnapshotBlock({
  title, snapshot, ports, assetUrl, entitiesByType, strict, t,
}: {
  title: string
  snapshot?: Record<string, unknown>
  ports: PipelinePort[]
  assetUrl?: AssetUrlFn
  entitiesByType: Record<string, EntityDict[]>
  /** strict:ports 白名单生效 → 只显示声明 port、且过滤后为空时整块不渲(而非显"暂无快照")。 */
  strict?: boolean
  t: TFn
}): ReactNode {
  const entries = snapshotEntries(snapshot, ports, strict)
  if (entries.length === 0) {
    // strict 下过滤为空 = 这块快照不在精简视图里 → 整块不渲(无空态占位)。
    if (strict) return null
    return (
      <section className="ze-pipeline__section">
        <h4 className="ze-text" data-role="title">{title}</h4>
        <div className="ze-text" data-role="tertiary" data-size="sm">{t('editor.pipelineNoSnapshot')}</div>
      </section>
    )
  }
  return (
    <section className="ze-pipeline__section">
      <h4 className="ze-text" data-role="title">{title}</h4>
      <div className="ze-pipeline__ports">
        {entries.map((entry) => (
          <PortValueRenderer
            key={entry.port.id}
            entry={entry}
            assetUrl={assetUrl}
            entitiesByType={entitiesByType}
            t={t}
          />
        ))}
      </div>
    </section>
  )
}

function LogsBlock({ logs, t }: { logs: PipelineLogEvent[]; t: TFn }): ReactNode {
  const logsRef = useScrollAffordance('y')
  if (logs.length === 0) return null
  return (
    <section className="ze-pipeline__section">
      <h4 className="ze-text" data-role="title">{t('editor.pipelineLogs')}</h4>
      <div ref={logsRef} className="ze-pipeline__logs ze-scroll-fade-y">
        {logs.map((l, i) => (
          <div key={`${l.ts}-${i}`} className="ze-pipeline__log ze-text" data-level={l.level} data-size="xs" data-mono="true">
            <span>{fmtTime(l.ts)}</span>
            <span>{l.level}</span>
            <span>{l.message}</span>
          </div>
        ))}
      </div>
    </section>
  )
}

/**
 * StartConsoleCell — 操作台「第一个 cell」(Jupyter 心智):params_schema 驱动参数表单 + 运行。
 * 渲染条件:view_state.startCommandId 非空且命令在 commandSpecs 里;否则整块不渲(向后兼容)。
 * 复用 .ze-pipeline__cell 外壳(data-cell="console" 区分)+ ParamPanel(schema 控件栈);
 * requires_runtime:dispatch 结果 op.error 结构化呈现(.ze-callout[danger] + .ze-kv-list);
 * 其余硬错误由 footer 错误条承接(useDocument 吞错入 errors,本 cell 不复述)。
 */
function StartConsoleCell({
  spec, view, run, selectedEntity, dispatch, t,
}: {
  spec: CommandSpecBrief
  view: Required<PipelineEditorViewState>
  run: PipelineRun | null
  selectedEntity: EntityDict | null
  dispatch?: DispatchFn
  t: TFn
}): ReactNode {
  // 按值键化(JSON)而非引用:宿主若逐 render 重建 viewState 字面量,startArgs/argsFromSelection
  // 引用会逐 render 翻新 → draftBase 翻新 → 下方 effect 把用户正在填的草稿冲掉。值不变则键不变。
  const startArgsKey = JSON.stringify(view.startArgs)
  const argsFromSelectionKey = JSON.stringify(view.argsFromSelection)
  const draftBase = useMemo(
    () => buildStartDraft(spec, view.startArgs, view.argsFromSelection, selectedEntity),
    [spec, startArgsKey, argsFromSelectionKey, selectedEntity],  // eslint-disable-line react-hooks/exhaustive-deps
  )
  const [draft, setDraft] = useState<Record<string, unknown>>(draftBase)
  const [inFlight, setInFlight] = useState(false)
  const [runtimeMissing, setRuntimeMissing] = useState<RequiresRuntime | null>(null)
  // 选中实体 / 命令变化 → 重置草稿与缺位提示(FormZone 同款受控约定)
  useEffect(() => { setDraft(draftBase); setRuntimeMissing(null) }, [draftBase])

  const properties = paramProperties(spec)
  const hasParams = Object.keys(properties).length > 0
  const paramSchema: EntityTypeSchema = { type: 'object', properties }
  const missing = missingRequiredParams(spec, draft)
  const active = runIsActive(run?.status)
  const busy = inFlight || active

  const onRun = async () => {
    setInFlight(true)
    setRuntimeMissing(null)
    try {
      const res = await Promise.resolve(dispatch?.(spec.id, { ...draft }))
      setRuntimeMissing(parseRequiresRuntime(res))
    } finally {
      setInFlight(false)
    }
  }

  // 操作台 cell = 信息+控制单元(同构二分):参数表单(ParamPanel)=信息区,Run 按钮=控制区。
  // Run 钮保留本地 inFlight/missing/requires_runtime 语义(非裸 dispatch 命令件,故不收编为
  // ToolbarAction);requires_runtime 结构化缺位呈现仍属信息(画在信息区底部)。
  return (
    <article className="ze-pipeline__cell ze-editor__unit" data-cell="console" data-phase={active ? 'running' : 'pending'}>
      <div className="ze-editor__unit-info" data-role="info">
        <header className="ze-pipeline__cell-head">
          <div>
            <strong>{t('editor.pipelineStartConsole')}</strong>
          </div>
          <div className="ze-pipeline__meta ze-text" data-role="tertiary" data-size="xs">
            <span>{spec.label || spec.id}</span>
          </div>
        </header>
        {spec.description ? <p className="ze-pipeline__desc">{spec.description}</p> : null}
        {hasParams ? (
          <ParamPanel
            schema={paramSchema}
            values={draft}
            onCommit={(field, value) => setDraft((d) => ({ ...d, [field]: value }))}
            readOnly={busy}
            requiredFields={requiredParamNames(spec)}
          />
        ) : null}
        {runtimeMissing ? (
          <>
            <div className="ze-callout" data-variant="danger">
              <strong className="ze-callout__title">{t('editor.pipelineRuntimeMissing')}</strong>
              <span className="ze-callout__body">{runtimeMissing.message}</span>
            </div>
            {(runtimeMissing.missing.length > 0 || runtimeMissing.hint) ? (
              <div className="ze-kv-list" data-size="sm">
                {runtimeMissing.missing.map((m) => (
                  <div key={m} className="ze-kv-list__row">
                    <span className="ze-kv-list__key">{t('editor.pipelineMissingLabel')}</span>
                    <span className="ze-kv-list__value" data-monospace="true">{m}</span>
                  </div>
                ))}
                {runtimeMissing.hint ? (
                  <div className="ze-kv-list__row">
                    <span className="ze-kv-list__key">{t('editor.pipelineHintLabel')}</span>
                    <span className="ze-kv-list__value">{runtimeMissing.hint}</span>
                  </div>
                ) : null}
              </div>
            ) : null}
          </>
        ) : null}
      </div>
      <div className="ze-editor__unit-control ze-pipeline__cell-actions" data-role="control">
        <button
          type="button" className="ze-button" data-size="sm" data-variant="primary"
          disabled={busy || missing.length > 0}
          title={missing.length > 0 ? t('slot.requiresFields', { fields: missing.join(', ') }) : undefined}
          onClick={() => void onRun()}
        >
          {busy ? t('editor.pipelineRunning') : t('editor.pipelineRun')}
        </button>
      </div>
    </article>
  )
}

/** 合并格(Jupyter 式取舍):整条管线合一成一格 —— 输入图 → 输出图 + 一个运行钮。
 *  输入/输出取**当前候选**的声明字段(版本下拉切历次,选中即当前结果);运行目标 = 选中候选的
 *  runTargetField(无候选则取 runTargetType 的唯一实体,供首跑);requires_runtime 内联提示
 *  (缺图像服务不静默)。逐步骤/日志/导出等细节收进「全量显示」。 */
function MergedCell({
  cell, activeEntity, candidates, entitiesByType, assetUrl, phase, dispatch, onSelect, t,
}: {
  cell: Required<PipelineCellSpec>
  activeEntity: EntityDict | null
  candidates: EntityDict[]
  entitiesByType: Record<string, EntityDict[]>
  assetUrl?: AssetUrlFn
  phase: string
  dispatch?: DispatchFn
  onSelect?: (id: string | null) => void
  t: TFn
}): ReactNode {
  const [inFlight, setInFlight] = useState(false)
  const [runtimeMissing, setRuntimeMissing] = useState<RequiresRuntime | null>(null)
  // busy 只看真在跑(inFlight=刚点 / live 'running'):phaseForEntityOrItem 对「无 live run 的实体」缺省
  // 返回 'pending',若把它当成在跑,已完成的候选(demo 一打开就有结果)会误显「运行中」。
  const busy = inFlight || phase === 'running'

  const inputSrc = valueToUrl(readEntityField(activeEntity, cell.inputField), assetUrl)
    || valueToUrl(readEntityField(activeEntity, cell.inputFallback), assetUrl)
  const outputSrc = valueToUrl(readEntityField(activeEntity, cell.outputField), assetUrl)
  // 格相位:在跑→running、有输出→done、否则→idle(未运行);不靠可能缺位的 live run 态。
  const cellPhase = busy ? 'running' : (outputSrc ? 'done' : 'idle')

  // 运行目标:选中候选的 sourceObjectId;无候选 → 文档里 runTargetType 的唯一实体(首跑)。
  const targetFromCand = asString(readEntityField(activeEntity, cell.runTargetField))
  const targetList = cell.runTargetType ? (entitiesByType[cell.runTargetType] ?? []) : []
  const targetId = targetFromCand || (targetList.length === 1 ? targetList[0].id : '')

  const onRun = async () => {
    if (!cell.runCommand || !targetId) return
    setInFlight(true)
    setRuntimeMissing(null)
    try {
      const res = await Promise.resolve(dispatch?.(cell.runCommand, { id: targetId }))
      setRuntimeMissing(parseRequiresRuntime(res))
    } finally {
      setInFlight(false)
    }
  }

  // 版本下拉:同元素历次候选(按 completedAt 升序 → 第 1/2… 次);选中即当前结果(onSelect)。
  const versions = candidates.slice().sort(
    (a, b) => (Number(readEntityField(a, 'completedAt')) || 0) - (Number(readEntityField(b, 'completedAt')) || 0))
  const versionSelect = versions.length > 1 ? (
    <span className="ze-select" data-size="sm">
      <select
        className="ze-select__control"
        value={activeEntity?.id ?? ''}
        onChange={(e) => onSelect?.(e.target.value)}
        aria-label={t('editor.pipelineVersionAria')}
      >
        {versions.map((c, i) => (
          <option key={c.id} value={c.id}>{t('editor.pipelineVersion', { n: i + 1 })}</option>
        ))}
      </select>
    </span>
  ) : null

  const ioCol = (label: string, src: string, isOutput: boolean) => (
    <div className="ze-pipeline__io-col">
      <div className="ze-pipeline__io-head">
        <span className="ze-text" data-role="tertiary" data-size="xs">{label}</span>
        {versionSelect}
      </div>
      {src
        ? <img className="ze-pipeline__io-img" src={src} alt={label} draggable={false} />
        : (
          <div className="ze-pipeline__io-empty ze-text" data-role="tertiary" data-size="sm">
            {isOutput && busy ? t('editor.pipelineRunning') : t('editor.pipelineNotRun')}
          </div>
        )}
    </div>
  )

  return (
    <article className="ze-pipeline__cell ze-editor__unit" data-cell="merged" data-phase={cellPhase}>
      <header className="ze-pipeline__cell-head">
        <span className="ze-pipeline__cell-headline">
          <span className="ze-badge ze-pipeline__badge" data-variant={phaseVariant(cellPhase)} data-phase={cellPhase} data-size="sm">
            {cellPhase === 'idle' ? t('editor.pipelineNotRun') : phaseLabel(cellPhase, t)}
          </span>
          <strong>{cell.labelKey ? t(cell.labelKey) : (cell.label || t('editor.pipeline'))}</strong>
        </span>
        <span className="ze-pipeline__cell-actions" data-role="control">
          <button
            type="button" className="ze-button" data-size="sm" data-variant="primary"
            disabled={busy || !targetId}
            onClick={() => void onRun()}
          >
            {busy ? t('editor.pipelineRunning') : (outputSrc ? t('editor.pipelineRerun') : t('editor.pipelineRun'))}
          </button>
        </span>
      </header>
      {runtimeMissing ? (
        <div className="ze-callout" data-variant="danger">
          <strong className="ze-callout__title">{t('editor.pipelineRuntimeMissing')}</strong>
          <span className="ze-callout__body">{runtimeMissing.message}</span>
        </div>
      ) : null}
      <div className="ze-pipeline__io">
        {ioCol(cell.inputLabelKey ? t(cell.inputLabelKey) : (cell.inputLabel || t('editor.pipelineInput')), inputSrc, false)}
        <span className="ze-pipeline__io-arrow" aria-hidden="true">→</span>
        {ioCol(cell.outputLabelKey ? t(cell.outputLabelKey) : (cell.outputLabel || t('editor.pipelineOutput')), outputSrc, true)}
      </div>
    </article>
  )
}

function Cell({
  run, step, index, logs, assetUrl, entitiesByType, command, stageMeta, dispatch, collapsed, onToggleCollapse, display, showAll, t,
}: {
  run: PipelineRun
  step: PipelineStepState
  index: number
  logs: PipelineLogEvent[]
  assetUrl?: AssetUrlFn
  entitiesByType: Record<string, EntityDict[]>
  command?: CommandSpecBrief
  stageMeta?: NonNullable<PipelineStaticMeta['stages']>[number]
  dispatch?: DispatchFn
  /** step-collapse:折叠态由编辑器持有(matrix 点格子 focus 时自动展开)。 */
  collapsed?: boolean
  onToggleCollapse?: () => void
  /** 显示开关(归一化);showAll=用户点了「全量显示」→ 无视开关全显示。 */
  display: Required<PipelineDisplayConfig>
  showAll: boolean
  t: TFn
}): ReactNode {
  const duration = fmtDuration(step.started_at, step.ended_at)
  // ports 白名单:showAll 或空白名单 → 全部;否则只留白名单内的 port。
  const portFilter = (ps: PipelinePort[]): PipelinePort[] =>
    showAll || display.ports.length === 0 ? ps : ps.filter((p) => display.ports.includes(p.id))
  const inputPorts = portFilter(portsForStep('input', step, command, stageMeta))
  const outputPorts = portFilter(portsForStep('output', step, command, stageMeta))
  const canControlStep = step.phase !== 'running' && step.phase !== 'pending'
  // step 生命周期命令(rerun/run_from)= 控制区的声明式命令件(ToolbarAction[kind=command],
  // 走 dispatch,禁裸 onClick)。args 同旧 fireStep(pipeline_id=run.id + item/stage)。
  const stepArgs = { pipeline_id: run.id, item_id: step.item_id, stage_id: step.stage_id }
  const stepControls: ToolbarAction[] = canControlStep ? [
    { id: 'rerun', labelKey: 'editor.pipelineRerunStep', kind: 'command', command: 'pipeline.rerun_step', args: stepArgs, variant: 'ghost' },
    { id: 'run-from', labelKey: 'editor.pipelineRunFromStep', kind: 'command', command: 'pipeline.run_from_step', args: stepArgs, variant: 'ghost' },
  ] : []
  // 每步「关闭/开启」断点(M1,类 jupyter「跑完此步停一停」):关闭=set halt-after,管线跑完此 stage
  // 边界 park(转 paused,既有「继续」=resume 续跑);开启=清断点自动续跑。armed 态从 run.breakpoints
  // (stage 级)读 → 同 stage 多 item 行状态一致;点击回传反转的 halt。仅多 stage 的非末步、run 仍可
  // 推进(running/paused/interrupted)时显——末步 halt-after 是 no-op、单 stage 不消费断点,故不显(免死钮)。
  const stageCount = run.stage_count ?? 0
  const stageHalted = (run.breakpoints ?? []).includes(step.stage_id)
  const runAdvanceable = run.status === 'running' || run.status === 'paused' || run.status === 'interrupted'
  const canArmBreakpoint = stageCount > 1 && step.stage_index !== undefined
    && step.stage_index < stageCount - 1 && runAdvanceable
  const breakpointControls: ToolbarAction[] = canArmBreakpoint ? [
    {
      id: 'breakpoint',
      labelKey: stageHalted ? 'editor.pipelineStepResumeFlow' : 'editor.pipelineStepHaltAfter',
      kind: 'command',
      command: 'pipeline.set_breakpoint',
      args: { pipeline_id: run.id, stage_id: step.stage_id, halt: !stageHalted },
      variant: stageHalted ? 'primary' : 'ghost',
    },
  ] : []
  const controlActions = [...breakpointControls, ...stepControls]
  // armed 断点常驻徽标(折叠/展开都显)——否则折叠态 cell 上的断点不可见。
  const breakpointBadge = stageHalted ? (
    <span className="ze-badge ze-pipeline__badge" data-variant="warn" data-size="sm">
      {t('editor.pipelineBreakpointBadge')}
    </span>
  ) : null
  if (collapsed) {
    return (
      <article className="ze-pipeline__cell ze-editor__unit" data-phase={step.phase} data-collapsed="true">
        <header className="ze-pipeline__cell-head">
          <button type="button" className="ze-disclosure" aria-expanded={false} onClick={onToggleCollapse}>
            <span className="ze-disclosure__caret ze-icon" data-icon="chevron-right" data-size="xs" aria-hidden="true" />
            <span className="ze-badge ze-pipeline__badge" data-variant={phaseVariant(step.phase)} data-phase={step.phase} data-size="sm">{phaseLabel(step.phase, t)}</span>
            {breakpointBadge}
            <strong>{t('editor.pipelineStep', { index: index + 1 })} · {command?.label || step.stage_label || step.command_id || step.stage_id}</strong>
          </button>
          <div className="ze-pipeline__meta ze-text" data-role="tertiary" data-size="xs">
            <span>{t('editor.pipelineStage')}: {step.stage_id}</span>
            {duration ? <span>{duration}</span> : null}
          </div>
        </header>
      </article>
    )
  }
  // cell = 信息+控制单元(同构二分):信息区(data-role=info)放 cell 现有自由内容
  // (head/progress/error/snapshots/logs),控制区(data-role=control)收 step 生命周期命令钮组。
  // 复用 .ze-editor__unit 词族(信息↔控制 border-top 发丝线);嵌套限一层(单元内不再嵌单元)。
  return (
    <article className="ze-pipeline__cell ze-editor__unit" data-phase={step.phase}>
      <div className="ze-editor__unit-info" data-role="info">
        <header className="ze-pipeline__cell-head">
          <button type="button" className="ze-disclosure" aria-expanded={true} onClick={onToggleCollapse}>
            <span className="ze-disclosure__caret ze-icon" data-icon="chevron-right" data-size="xs" aria-hidden="true" />
            <span className="ze-badge ze-pipeline__badge" data-variant={phaseVariant(step.phase)} data-phase={step.phase} data-size="sm">{phaseLabel(step.phase, t)}</span>
            {breakpointBadge}
            <strong>{t('editor.pipelineStep', { index: index + 1 })} · {command?.label || step.stage_label || step.command_id || step.stage_id}</strong>
          </button>
          <div className="ze-pipeline__meta ze-text" data-role="tertiary" data-size="xs">
            <span>{t('editor.pipelineStage')}: {step.stage_id}</span>
            {duration ? <span>{duration}</span> : null}
            {step.progress !== undefined && step.progress !== null ? <span>{pct(step.progress)}</span> : null}
          </div>
        </header>
        {/* progress==null = 进度未知 → indeterminate 来回平移;有数值(含 0)= 确定进度 → inline width。 */}
        {step.phase === 'running' && (
          <div className="ze-progress" data-indeterminate={step.progress == null ? 'true' : undefined}>
            <div
              className="ze-progress__fill"
              style={step.progress == null ? undefined : { width: pct(step.progress) }}
            />
          </div>
        )}
        {step.error && (
          <div className="ze-callout" data-variant="danger">
            <strong className="ze-callout__title">{step.error.code || 'error'}</strong>
            <span className="ze-callout__body">{step.error.message || t('editor.pipelineStepFailed')}</span>
          </div>
        )}
        {command?.description ? <p className="ze-pipeline__desc">{command.description}</p> : null}
        {(showAll || display.showInputs) ? (
          <SnapshotBlock
            title={t('editor.pipelineInputs')}
            snapshot={step.input_snapshot}
            ports={inputPorts}
            assetUrl={assetUrl}
            entitiesByType={entitiesByType}
            strict={!showAll && display.ports.length > 0}
            t={t}
          />
        ) : null}
        {(showAll || display.showOutputs) ? (
          <SnapshotBlock
            title={t('editor.pipelineOutputs')}
            snapshot={step.output_snapshot}
            ports={outputPorts}
            assetUrl={assetUrl}
            entitiesByType={entitiesByType}
            strict={!showAll && display.ports.length > 0}
            t={t}
          />
        ) : null}
        {(showAll || display.showOperations) && step.operation_ids && step.operation_ids.length > 0 ? (
          <section className="ze-pipeline__section">
            <h4 className="ze-text" data-role="title">{t('editor.pipelineOperations')}</h4>
            <div className="ze-pipeline__chips">
              {step.operation_ids.map((id) => <span key={id} className="ze-badge" data-size="sm">{id}</span>)}
            </div>
          </section>
        ) : null}
        {(showAll || display.showLogs) ? <LogsBlock logs={logs} t={t} /> : null}
      </div>
      {controlActions.length > 0 ? (
        <div className="ze-editor__unit-control ze-pipeline__cell-actions" data-role="control">
          {controlActions.map((a) => renderToolbarAction(a, t, dispatch, undefined, undefined))}
        </div>
      ) : null}
    </article>
  )
}

// phaseFromSteps 抽到 lib/pipelineChain.ts(单一真相源:entity 级与 chain command 合并级共用次序)。

function phaseForEntity(entity: EntityDict | null, steps: PipelineStepState[], itemIdField: string, statusOf?: StatusLookup): string {
  if (!entity) return 'pending'
  const status = statusOf?.(entity.type, entity.id)
  if (status?.phase) return status.phase
  const itemId = asString(readEntityField(entity, itemIdField)) || entity.id
  const owned = steps.filter((s) => s.entity_id === entity.id || s.item_id === itemId || s.item_id === entity.id)
  return phaseFromSteps(owned)
}

function phaseForEntityOrItem(
  entity: EntityDict | null,
  itemId: string,
  steps: PipelineStepState[],
  itemIdField: string,
  statusOf?: StatusLookup,
): string {
  if (entity) return phaseForEntity(entity, steps, itemIdField, statusOf)
  if (!itemId) return phaseFromSteps(steps)
  return phaseFromSteps(steps.filter((s) => s.item_id === itemId))
}

function entityLabel(entity: EntityDict | null, ui: PipelineEntityUi, fallback: string): string {
  const v = readEntityField(entity, ui.labelField)
  return asString(v) || entity?.name || fallback
}

function entityThumb(entity: EntityDict | null, ui: PipelineEntityUi, assetUrl?: AssetUrlFn): string {
  const field = ui.thumbField || ui.resultField
  return valueToUrl(readEntityField(entity, field), assetUrl)
}

function entityByItemId(entities: EntityDict[], itemId: string, itemIdField: string): EntityDict | null {
  return entities.find((e) => asString(readEntityField(e, itemIdField)) === itemId || e.id === itemId) ?? null
}

function stageIdsInOrder(steps: PipelineStepState[]): string[] {
  const pairs = new Map<string, number>()
  for (const s of steps) {
    const current = pairs.get(s.stage_id)
    const idx = s.stage_index ?? 9999
    if (current === undefined || idx < current) pairs.set(s.stage_id, idx)
  }
  return Array.from(pairs.entries()).sort((a, b) => a[1] - b[1]).map(([id]) => id)
}

/** 静态 stage 列表(无 run 时的常驻骨架源):按 view.pipelineId 在 pipelineSpecs 取声明的 stages。
 *  纯派生、无副作用(可单测);mod 未声明 stages 或非固定管线槽 → 空 → 骨架退化为"尚未运行"提示。 */
export function staticStagesFor(
  pipelineSpecs: PipelineStaticMeta[],
  pipelineId: string | undefined,
): Array<{ id: string; label?: string }> {
  if (!pipelineId) return []
  return pipelineSpecs.find((p) => p.id === pipelineId)?.stages ?? []
}

/** 无 run 时的 stage 骨架列:从静态 stages 渲一列 pending 占位 cell(块壳 .ze-editor__unit 同真 cell,
 *  首次 Run 原地替换 → 零 reflow)。静态源缺 → 退化为一行"尚未运行"提示(向后兼容旧槽)。 */
function StageSkeletonColumn(
  { stages, t }: { stages: Array<{ id: string; label?: string }>; t: TFn },
): ReactNode {
  if (stages.length === 0) {
    return <div className="ze-text" data-role="tertiary" data-size="sm">{t('editor.pipelineNoRun')}</div>
  }
  return (
    <>
      {stages.map((st, i) => (
        <article key={st.id} className="ze-pipeline__cell ze-editor__unit" data-phase="idle">
          <div className="ze-editor__unit-info" data-role="info">
            <header className="ze-pipeline__cell-head">
              <span className="ze-pipeline__cell-headline">
                <span className="ze-badge ze-pipeline__badge" data-size="sm">{t('editor.pipelineNotRun')}</span>
                <strong>{t('editor.pipelineStep', { index: i + 1 })} · {st.label || st.id}</strong>
              </span>
            </header>
          </div>
        </article>
      ))}
    </>
  )
}

// ============================================================
// 链路主视图(对象组 → command → 对象组)。view_state.chain 有效时取代 notebook 作默认主视图;
// 对象组只组合既有展示语言(.ze-kv-list / preview / ThumbnailGrid),command 走 InfoControlUnit 二分。
// ============================================================

/** 对象组(链路节点的数据态):只描述「此处有什么」,复用既有展示语言,不带业务按钮、不自画黑边卡片。
 *  状态来自 entity status(data-run-phase),在自身 footprint 内进入状态(不造专属 loading/error 框)。 */
function PipelineObjectGroup({
  resolved, assetUrl, statusOf, statusAnimation, schemas, uploadAsset, dispatch, onSelect, t,
}: {
  resolved: ResolvedPipelineObjectGroup
  assetUrl?: AssetUrlFn
  statusOf?: StatusLookup
  statusAnimation?: string
  schemas?: Record<string, EntityTypeSchema | undefined>
  uploadAsset?: (bytes: Uint8Array, mime: string, ext?: string) => Promise<AssetRefDict>
  dispatch?: DispatchFn
  onSelect?: (id: string | null) => void
  t: TFn
}): ReactNode {
  const { spec, entity, entities, missing } = resolved
  const display = spec.display
  const phase = entity ? statusOf?.(entity.type, entity.id)?.phase : undefined

  const kvList = (fields: string[]): ReactNode => {
    const rows = fields
      .map((f) => ({ field: f, value: readGroupField(resolved, f) }))
      // 跳过空值与对象(asset ref 等):kv 只放标量;资产用 preview/assetPreviews 展示,不渲 "[object Object]"。
      .filter((r) => r.value !== undefined && r.value !== null && r.value !== '' && typeof r.value !== 'object')
      .map((r) => ({ field: r.field, label: humanizeField(r.field), value: formatSummaryValue(r.field, r.value) }))
    if (rows.length === 0) return null
    return (
      <div className="ze-kv-list" data-variant="compact" data-size="sm">
        {rows.map((r) => (
          <div key={r.field} className="ze-kv-list__row">
            <span className="ze-kv-list__key">{r.label}</span>
            <span className="ze-kv-list__value">{r.value}</span>
          </div>
        ))}
      </div>
    )
  }
  const previewImg = (field: string, fallbackField?: string): ReactNode => {
    const url = valueToUrl(readGroupField(resolved, field), assetUrl)
      || (fallbackField ? valueToUrl(readGroupField(resolved, fallbackField), assetUrl) : '')
    if (!url) return null
    return <img className="ze-pipeline__object-preview" src={url} alt="" draggable={false} />
  }

  let body: ReactNode
  if (missing) {
    body = <div className="ze-text" data-role="tertiary" data-size="sm">{t('editor.pipelineNotRun')}</div>
  } else {
    switch (display.kind) {
      case 'empty':
        body = <div className="ze-text" data-role="tertiary" data-size="sm">{display.textKey ? t(display.textKey) : t('editor.pipelineNotRun')}</div>
        break
      case 'preview':
        body = previewImg(display.field, display.fallbackField)
        break
      case 'previewKv':
        body = <>{previewImg(display.previewField)}{kvList(display.fields)}</>
        break
      case 'assetPreviews':
        body = (
          <>
            {display.fields.map((f) => {
              const url = valueToUrl(readGroupField(resolved, f), assetUrl)
              return url ? <img key={f} className="ze-pipeline__object-preview" src={url} alt="" draggable={false} /> : null
            })}
          </>
        )
        break
      case 'thumbnailList': {
        const items: ThumbItem[] = entities.map((e) => ({
          id: e.id,
          src: valueToUrl(readEntityField(e, display.thumbField), assetUrl) || undefined,
          label: asString(readEntityField(e, display.labelField || 'name')) || e.name || e.id,
        }))
        body = (
          <ThumbnailGrid
            items={items}
            selectedId={entity?.id ?? null}
            onSelect={(id) => onSelect?.(id)}
            layoutMode="compact-list"
            thumbSize="sm"
          />
        )
        break
      }
      case 'fields':
      case 'kv':
        body = kvList(display.fields)
        break
      case 'form': {
        // 可编辑节点(反哺 B):把链路节点实体就地做成可改字段 —— 复用 L3 FormZone(子 schema 只含
        // 声明字段;按字段 schema 类型派生控件:标量失焦走 entity.update CAS、asset 走 AssetRefInput
        // 上传,与属性面板同机制)。让管线编辑器既能显示也能就地编辑该节点实体(如项目设置),无需
        // 另开属性面板。FieldRow(L3)消费 asset primitive,L4 不越层 import。
        if (!entity) { body = null; break }
        const fullSchema = schemas?.[entity.type]
        const subSchema: EntityTypeSchema = {
          type: 'object',
          properties: Object.fromEntries(display.fields.map((f) => [f, fullSchema?.properties?.[f] ?? {}])),
        }
        body = (
          <FormZone
            entity={entity} schema={subSchema} dispatch={dispatch}
            assetUrl={assetUrl} uploadAsset={uploadAsset}
          />
        )
        break
      }
      default:
        body = null
    }
  }

  const groupLabel = spec.labelKey ? t(spec.labelKey) : spec.label
  return (
    <section className="ze-pipeline__group" data-run-phase={phase} data-animation={statusAnimation} aria-label={groupLabel}>
      {groupLabel ? <h4 className="ze-text" data-role="title" data-size="sm">{groupLabel}</h4> : null}
      <div className="ze-pipeline__object-fields">{body}</div>
    </section>
  )
}

/** command 单元(链路加工动作):InfoControlUnit 二分 —— 信息区放 label/相位/进度/错误,控制区收 Run/Rerun。
 *  相位/进度/错误来自合并 step_states(stage_id 闭集);参数缺位 → 控制钮置灰示因(不夹进信息区)。 */
function PipelineCommandUnit({
  step, steps, groupEntities, activeEntity, dispatch, t,
}: {
  step: PipelineCommandStepSpec
  steps: PipelineStepState[]
  groupEntities: Record<string, EntityDict | null>
  activeEntity: EntityDict | null
  dispatch?: DispatchFn
  t: TFn
}): ReactNode {
  const [inFlight, setInFlight] = useState(false)
  const [runtimeMissing, setRuntimeMissing] = useState<RequiresRuntime | null>(null)
  const phase = phaseForMergedStages(steps, step.stageIds, step.commandId)
  const progress = progressForMergedStages(steps, step.stageIds, step.commandId)
  const error = errorForMergedStages(steps, step.stageIds, step.commandId)
  const { payload, blockedReasons } = resolveStepArgs(step, { activeEntity, groupEntities })
  const running = inFlight || phase === 'running'
  const blocked = blockedReasons.length > 0
  const dphase = running ? 'running' : phase
  const runLabel = running
    ? t('editor.pipelineRunning')
    : (phase === 'done' || phase === 'error') ? t('editor.pipelineRerun') : t('editor.pipelineRun')

  const onRun = async () => {
    if (blocked) return
    setInFlight(true)
    setRuntimeMissing(null)
    try {
      const res = await Promise.resolve(dispatch?.(step.commandId, payload))
      setRuntimeMissing(parseRequiresRuntime(res))
    } finally {
      setInFlight(false)
    }
  }

  return (
    <article className="ze-pipeline__command ze-editor__unit" data-run-phase={dphase}>
      <div className="ze-editor__unit-info" data-role="info">
        <header className="ze-pipeline__command-head">
          <span className="ze-badge ze-pipeline__badge" data-variant={phaseVariant(dphase)} data-phase={dphase} data-size="sm">
            {dphase === 'idle' ? t('editor.pipelineNotRun') : phaseLabel(dphase, t)}
          </span>
          <strong>{step.labelKey ? t(step.labelKey) : step.label}</strong>
        </header>
        {running ? (
          <div className="ze-progress" data-indeterminate={progress == null ? 'true' : undefined}>
            <div className="ze-progress__fill" style={progress == null ? undefined : { width: pct(progress) }} />
          </div>
        ) : null}
        {error ? (
          <div className="ze-callout" data-variant="danger">
            <strong className="ze-callout__title">{error.code}</strong>
            <span className="ze-callout__body">{error.message}</span>
          </div>
        ) : null}
        {runtimeMissing ? (
          <div className="ze-callout" data-variant="danger">
            <strong className="ze-callout__title">{t('editor.pipelineRuntimeMissing')}</strong>
            <span className="ze-callout__body">{runtimeMissing.message}</span>
          </div>
        ) : null}
      </div>
      <div className="ze-editor__unit-control ze-pipeline__command-actions" data-role="control">
        <button
          type="button" className="ze-button" data-size="sm" data-variant="primary"
          disabled={running || blocked}
          title={blocked ? blockedReasons.join('; ') : undefined}
          onClick={() => void onRun()}
        >{runLabel}</button>
      </div>
    </article>
  )
}

/** 链路主视图:对象组 → command → 对象组。解算各对象组与 command 参数上下文,按 step.from/to 串联。 */
function PipelineChainView({
  chain, activeEntity, entitiesByType, entities, steps, assetUrl, statusOf, statusAnimation, schemas, uploadAsset, dispatch, onSelect, t,
}: {
  chain: ParsedPipelineChain
  activeEntity: EntityDict | null
  entitiesByType: Record<string, EntityDict[]>
  entities: EntityDict[]
  steps: PipelineStepState[]
  assetUrl?: AssetUrlFn
  statusOf?: StatusLookup
  statusAnimation?: string
  schemas?: Record<string, EntityTypeSchema | undefined>
  uploadAsset?: (bytes: Uint8Array, mime: string, ext?: string) => Promise<AssetRefDict>
  dispatch?: DispatchFn
  onSelect?: (id: string | null) => void
  t: TFn
}): ReactNode {
  const ctx: ResolveGroupContext = { activeEntity, entitiesByType, entities, steps }
  const resolvedByGroup = new Map<string, ResolvedPipelineObjectGroup>()
  for (const g of chain.groups) resolvedByGroup.set(g.id, resolvePipelineObjectGroup(g, ctx))
  const groupEntities: Record<string, EntityDict | null> = {}
  resolvedByGroup.forEach((r, id) => { groupEntities[id] = r.entity })

  const groupNode = (id: string): ReactNode => {
    const r = resolvedByGroup.get(id)
    if (!r) return null
    return <PipelineObjectGroup key={`g-${id}`} resolved={r} assetUrl={assetUrl} statusOf={statusOf} statusAnimation={statusAnimation} schemas={schemas} uploadAsset={uploadAsset} dispatch={dispatch} onSelect={onSelect} t={t} />
  }
  const connector = (key: string): ReactNode => <div key={key} className="ze-pipeline__connector" aria-hidden="true" />

  const nodes: ReactNode[] = []
  const rendered = new Set<string>()
  if (chain.groups[0]) {
    nodes.push(groupNode(chain.groups[0].id))
    rendered.add(chain.groups[0].id)
  }
  chain.steps.forEach((s, i) => {
    nodes.push(connector(`ca-${i}`))
    nodes.push(
      <PipelineCommandUnit
        key={`s-${s.id}`}
        step={s}
        steps={steps}
        groupEntities={groupEntities}
        activeEntity={activeEntity}
        dispatch={dispatch}
        t={t}
      />,
    )
    if (!rendered.has(s.to)) {
      nodes.push(connector(`cb-${i}`))
      nodes.push(groupNode(s.to))
      rendered.add(s.to)
    }
  })
  for (const g of chain.groups) {
    if (!rendered.has(g.id)) nodes.push(groupNode(g.id))
  }

  return <div className="ze-pipeline__chain">{nodes}</div>
}

export function PipelineEditor({
  editorName,
  entityType, entities, selectedId, selected, viewState, pipelineRuns = [],
  commandSpecs = [], pipelineSpecs = [], entitiesByType, statusOf, assetUrl, schemas, uploadAsset, dispatch, onSelect,
}: PipelineEditorProps): ReactNode {
  const t = useT()
  const rootRef = useScrollAffordance('y')
  const matrixRef = useScrollAffordance('x')
  const view = useMemo(() => parsePipelineViewState(viewState), [viewState])
  const selectedEntity = selected ?? entities.find((e) => e.id === selectedId) ?? null
  const [manualRunId, setManualRunId] = useState<string>('')
  const [focusKey, setFocusKey] = useState<string>('')
  // 全量显示:用户一键覆盖 display 开关、看全部(默认按 mod 声明的精简显示)。
  const [showAll, setShowAll] = useState(false)
  // 切换实体(选另一候选/历史)时重置回精简显示 —— 否则在 A 点了全量,切到 B 还是全量,违背默认意图。
  useEffect(() => { setShowAll(false) }, [selectedId])
  // step-collapse:折叠 cell 集(key=stepKey);默认全展开,matrix 点格子 focus 自动展开目标
  const [collapsedKeys, setCollapsedKeys] = useState<ReadonlySet<string>>(new Set())
  const toggleCollapse = (key: string) => setCollapsedKeys((prev) => {
    const next = new Set(prev)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    return next
  })
  const commands = useMemo(() => commandMap(commandSpecs), [commandSpecs])

  const run = useMemo(
    () => selectPipelineRun(pipelineRuns, selectedEntity, view, manualRunId),
    [manualRunId, pipelineRuns, selectedEntity, view],
  )
  const steps = useMemo(() => run?.step_states ?? [], [run])
  const itemIds = Array.from(new Set(steps.map((s) => s.item_id)))
  const stageIds = stageIdsInOrder(steps)
  const itemIdField = view.entityUi.itemIdField || view.itemIdField
  const selectedBelongsToRun = run ? runHasEntity(run, selectedEntity, itemIdField) : false
  const activeItemId = selectedBelongsToRun
    ? asString(readEntityField(selectedEntity, itemIdField)) || selectedEntity?.id || itemIds[0] || ''
    : itemIds[0] || ''
  const activeEntity = selectedBelongsToRun
    ? selectedEntity
    : entityByItemId(entities, activeItemId, itemIdField)
  const visibleSteps = steps
    .filter((s) => itemIds.length <= 1 || !activeItemId || s.item_id === activeItemId || stepKey(s) === focusKey)
    .sort((a, b) => (a.stage_index ?? 0) - (b.stage_index ?? 0))
  const focus = focusKey ? steps.find((s) => stepKey(s) === focusKey) : null
  const notebookSteps = focus ? [focus] : visibleSteps
  const allEntities = entitiesByType ?? { [entityType]: entities }
  const runPayload = run ? { pipeline_id: run.id } : {}
  const title = editorName || view.pipelineId || view.startCommandId || run?.name || t('editor.pipeline')
  const activePhase = phaseForEntityOrItem(activeEntity, activeItemId, steps, itemIdField, statusOf)
  const activeThumb = entityThumb(activeEntity, view.entityUi, assetUrl)

  // 操作台:startCommandId 声明 + 命令在册才渲(向后兼容:未声明的旧槽零变化)
  const consoleSpec = view.startCommandId ? commands.get(view.startCommandId) : undefined
  // 无 run 时的常驻骨架源:静态 stage 列表(view.pipelineId → pipelineSpecs.stages)
  const staticStages = useMemo(() => staticStagesFor(pipelineSpecs, view.pipelineId), [pipelineSpecs, view.pipelineId])
  // 摘要 entity 字段 → 统一 kv 行(可读 label + 格式化值,空值剔除);不再裸 "field: value" span。
  const summaryRows = !run ? [] : (view.entityUi.summaryFields ?? [])
    .map((field) => ({ field, value: readEntityField(activeEntity, field) }))
    .filter((r) => r.value !== undefined && r.value !== null && r.value !== '')
    .map((r) => ({ field: r.field, label: humanizeField(r.field), value: formatSummaryValue(r.field, r.value) }))

  // 每元素运行历史:同源物体的多次结果(候选),按 run started_at 倒序(最新在前)→ 缩略图卡列表。
  // >1 才渲(有得切换才显示);切换=选中该候选、删除=entity.delete 软删(进 OpLog,可全局撤)。
  const historyCands = elementCandidates(entities, activeEntity, view.historyKeyField)
  // 合并视图(Jupyter 式干净面板):声明了 display.cells 且未点「全量显示」→ 只渲合并格(输入→输出+控制),
  // 把 StartConsole/摘要/历史条/逐步 cell/导出 全收进「全量显示」。
  const mergedMode = view.display.cells.length > 0 && !showAll
  const runStartedAt = (cand: EntityDict): number =>
    pipelineRuns.find((r) => r.id === asString(readEntityField(cand, view.runIdField)))?.started_at ?? 0
  const historyItems: ThumbItem[] = view.historyKeyField && historyCands.length > 1
    ? historyCands.slice().sort((a, b) => runStartedAt(b) - runStartedAt(a)).map((cand) => {
        const st = statusOf?.(cand.type, cand.id)
        const ph = st?.phase
        return {
          id: cand.id,
          src: entityThumb(cand, view.entityUi, assetUrl) || undefined,
          label: entityLabel(cand, view.entityUi, cand.id),
          status: ph === 'running' || ph === 'pending' ? 'loading' : ph === 'error' ? 'error' : undefined,
          error: st?.error,
        } satisfies ThumbItem
      })
    : []

  // run 生命周期命令 = 控制区声明式命令件(ControlZone:ToolbarAction[kind=command] → dispatch)。
  // 可见性沿用 run.can_*;retry_failed 仍守「run 停下才出现」(后端对活跃 run 是 no-op,避免假交互,
  // 2026-06-11 真浏览器回归抓出)。导出 notebook 是纯前端只读序列化(SPEC §pipeline:无命令),
  // 不能收编为 dispatch ToolbarAction,故另成一条只读导出控制行(仍 data-role=control)。
  const lifecycleControls: ToolbarAction[] = !run ? [] : [
    ...(run.can_pause ? [{ id: 'pause', labelKey: 'editor.pause', kind: 'command', command: 'pipeline.pause', args: runPayload } as ToolbarAction] : []),
    ...(run.can_interrupt ? [{ id: 'interrupt', labelKey: 'editor.pipelineInterrupt', kind: 'command', command: 'pipeline.interrupt', args: runPayload } as ToolbarAction] : []),
    ...(run.can_resume ? [{ id: 'resume', labelKey: 'editor.pipelineContinue', kind: 'command', command: 'pipeline.resume', args: runPayload } as ToolbarAction] : []),
    ...(run.can_cancel ? [{ id: 'cancel', labelKey: 'editor.cancel', kind: 'command', command: 'pipeline.cancel', args: runPayload, variant: 'ghost' } as ToolbarAction] : []),
    ...(run.failed_count && !runIsActive(run.status) ? [{ id: 'retry', labelKey: 'editor.pipelineRetryFailed', kind: 'command', command: 'pipeline.retry_failed', args: runPayload } as ToolbarAction] : []),
  ]

  const headerZone = <HeaderZone title={title} />
  const editorZones: ReactNode[] = []
  if (view.allowRunSwitch) {
    // run selector = 视图态切换控件(<select>),非生命周期命令、非身份(header 禁输入控件,
    // SPEC ZONE_HEADER_PURE);下沉到 toolbar-role 行,不进 header。
    editorZones.push(
      <FunctionZone key="run-select" id="toolbar" role="toolbar" kind="tool" layout="hug-scroll">
        <span className="ze-select" data-size="sm">
          <select className="ze-select__control" value={run?.id ?? ''} onChange={(e) => { setManualRunId(e.target.value); setFocusKey('') }}>
            {pipelineRuns.length === 0 ? <option value="">{t('editor.pipelineNoRun')}</option> : null}
            {pipelineRuns.map((r) => <option key={r.id} value={r.id}>{r.name || r.id}</option>)}
          </select>
        </span>
      </FunctionZone>,
    )
  }
  if (lifecycleControls.length > 0) {
    editorZones.push(<ControlZone key="lifecycle" actions={lifecycleControls} dispatch={dispatch} />)
  }
  // 导出 notebook 控制行:display.showExport=false 的纯展示工具(如 object-finalizer)整块不渲。
  if (run && view.display.showExport) {
    editorZones.push(
      <FunctionZone key="export" id="control" role="control" kind="tool" layout="hug-scroll">
        <button type="button" className="ze-button" data-size="sm" data-variant="ghost" onClick={() => downloadText(notebookToJson(run, commands), `${run.id}.notebook.json`, 'application/json')}>{t('editor.pipelineExportJson')}</button>
        <button type="button" className="ze-button" data-size="sm" data-variant="ghost" onClick={() => downloadText(notebookToMarkdown(run, commands), `${run.id}.notebook.md`, 'text/markdown')}>{t('editor.pipelineExportMd')}</button>
      </FunctionZone>,
    )
  }

  // 合并格(legacy)与完整 notebook 抽为变量:notebook 既是 legacy 主体,也是 chain 模式「展开详情」的内容。
  const mergedCells = (
    <div className="ze-pipeline__cells">
      {view.display.cells.map((cell, i) => (
        <MergedCell
          key={cell.label || String(i)}
          cell={cell}
          activeEntity={activeEntity}
          candidates={historyCands}
          entitiesByType={allEntities}
          assetUrl={assetUrl}
          phase={activePhase}
          dispatch={dispatch}
          onSelect={(id) => onSelect?.(id)}
          t={t}
        />
      ))}
    </div>
  )
  const notebookBody = (
          <>
          {consoleSpec ? (
            <StartConsoleCell
              spec={consoleSpec} view={view} run={run}
              selectedEntity={selectedEntity} dispatch={dispatch} t={t}
            />
          ) : null}
          {/* 运行摘要 + entity 块常驻:有 run(填真态)或已有绑定 entity 时渲;无 run 时相位由
              activeEntity/空 steps 派生(新建候选即 pending)、status 文案=未运行、不堆 run 态字段
              (stage/eta/summaryFields)。首次 Run 原地填真数据,块壳不变 → 视觉无尺寸抖动。 */}
          {(run || activeEntity) ? (
          <div className="ze-pipeline__summary" data-state={run?.status ?? 'idle'}>
            <div className="ze-pipeline__entity" data-phase={activePhase} data-animation={view.entityUi.statusAnimation}>
              {activeThumb ? <img className="ze-pipeline__entity-thumb" src={activeThumb} alt="" draggable={false} /> : null}
              <span className="ze-pipeline__entity-main">
                <strong>{entityLabel(activeEntity, view.entityUi, activeItemId || entityType)}</strong>
                <span className="ze-text" data-role="tertiary" data-size="xs">{run ? `${run.status} · ${pct(run.progress) || run.stage}` : t('editor.pipelineNoRun')}</span>
              </span>
            </div>
            {run ? <span>{run.stage}</span> : null}
            {run?.eta_sec ? <span>{t('footer.etaLabel')} {Math.round(run.eta_sec)}s</span> : null}
            {run?.status === 'interrupted' ? <span>{t('editor.pipelineInterruptedHint')}</span> : null}
          </div>
          ) : null}

          {/* entity 摘要字段 → 框架统一 kv-list(紧凑、对齐、可读 label + 格式化值;同 inspector 渲染语言)。
              不再"字段名当 label / 不格式化 / 裸 span 浪费面积"。 */}
          {summaryRows.length > 0 ? (
            <div className="ze-kv-list" data-variant="compact" data-size="sm">
              {summaryRows.map((r) => (
                <div key={r.field} className="ze-kv-list__row">
                  <span className="ze-kv-list__key">{r.label}</span>
                  <span className="ze-kv-list__value">{r.value}</span>
                </div>
              ))}
            </div>
          ) : null}

          {/* 每元素运行历史:同源物体多次结果(候选)→ 共享 ThumbnailGrid 列表;点卡切换(选中该候选)、
              删卡走 entity.delete 软删(进 OpLog 可全局撤);当前候选高亮。最新在前。 */}
          {historyItems.length > 0 ? (
            <section className="ze-pipeline__section">
              <h4 className="ze-text" data-role="title">{t('editor.pipelineRunHistory')}</h4>
              <ThumbnailGrid
                items={historyItems}
                selectedId={activeEntity?.id ?? null}
                onSelect={(id) => onSelect?.(id)}
                onDelete={(id) => {
                  // 删的是当前候选 → 先切到另一历史候选(列表 >1 才渲,删后必有剩余),避免残留在已删视图;
                  // 再 entity.delete 软删(进 OpLog 可全局撤)。带 expected_version 走 CAS(铁律 12,
                  // 与 TableZone/GridZone/TimelineZone 一致;候选在 historyCands 里取版本)。
                  if (id === activeEntity?.id) {
                    const next = historyItems.find((it) => it.id !== id)
                    if (next) onSelect?.(next.id)
                  }
                  const cand = historyCands.find((c) => c.id === id)
                  dispatch?.('entity.delete', { type: entityType, id, expected_version: cand?.version })
                }}
                layoutMode="compact-list"
                thumbSize="sm"
              />
            </section>
          ) : null}

          {run && itemIds.length > 1 && (
            <div ref={matrixRef} className="ze-pipeline__matrix ze-scroll-fade-x">
              <div className="ze-pipeline__matrix-head" style={{ gridTemplateColumns: `160px repeat(${stageIds.length}, minmax(96px, 1fr))` }}>
                <span>{entityType}</span>
                {stageIds.map((sid) => <span key={sid}>{sid}</span>)}
              </div>
              {itemIds.map((iid) => {
                const ent = entityByItemId(entities, iid, itemIdField)
                const phase = phaseForEntityOrItem(ent, iid, steps, itemIdField, statusOf)
                return (
                  <div
                    key={iid}
                    className="ze-pipeline__matrix-row"
                    data-phase={phase}
                    data-animation={view.entityUi.statusAnimation}
                    style={{ gridTemplateColumns: `160px repeat(${stageIds.length}, minmax(96px, 1fr))` }}
                  >
                    <button type="button" onClick={() => onSelect?.(ent?.id ?? null)}>{entityLabel(ent, view.entityUi, iid)}</button>
                    {stageIds.map((sid) => {
                      const s = steps.find((x) => x.item_id === iid && x.stage_id === sid)
                      return (
                        <button
                          key={sid}
                          type="button"
                          data-phase={s?.phase ?? 'pending'}
                          onClick={() => {
                            if (!s) return
                            const k = stepKey(s)
                            setFocusKey(k)
                            // focus 即查看详情:目标 cell 若被折叠自动展开
                            setCollapsedKeys((prev) => {
                              if (!prev.has(k)) return prev
                              const next = new Set(prev)
                              next.delete(k)
                              return next
                            })
                          }}
                        >{phaseLabel(s?.phase ?? 'pending', t)}</button>
                      )
                    })}
                  </div>
                )
              })}
              {focusKey ? <button type="button" className="ze-button" data-size="sm" data-variant="ghost" onClick={() => setFocusKey('')}>{t('editor.pipelineShowItemNotebook')}</button> : null}
            </div>
          )}

          <div className="ze-pipeline__cells">
            {!run ? (
              <StageSkeletonColumn stages={staticStages} t={t} />
            ) : notebookSteps.length === 0 ? (
              <div className="ze-text" data-role="tertiary" data-size="sm">{t('editor.pipelineNoSteps')}</div>
            ) : notebookSteps.map((s, i) => {
              const logs = (run.log_events ?? []).filter((l) =>
                (!l.item_id || l.item_id === s.item_id) && (!l.stage_id || l.stage_id === s.stage_id))
              return (
                <Cell
                  key={stepKey(s)}
                  run={run}
                  step={s}
                  index={s.stage_index ?? i}
                  logs={logs}
                  assetUrl={assetUrl}
                  entitiesByType={allEntities}
                  command={commands.get(s.command_id)}
                  stageMeta={stageMetaFor(pipelineSpecs, run, s)}
                  dispatch={dispatch}
                  collapsed={collapsedKeys.has(stepKey(s))}
                  onToggleCollapse={() => toggleCollapse(stepKey(s))}
                  display={view.display}
                  showAll={showAll}
                  t={t}
                />
              )
            })}
          </div>
          </>
  )
  const chainView = view.chain ? (
    <PipelineChainView
      chain={view.chain}
      activeEntity={activeEntity}
      entitiesByType={allEntities}
      entities={entities}
      steps={steps}
      assetUrl={assetUrl}
      statusOf={statusOf}
      statusAnimation={view.entityUi.statusAnimation}
      schemas={schemas}
      uploadAsset={uploadAsset}
      dispatch={dispatch}
      onSelect={onSelect}
      t={t}
    />
  ) : null

  return (
    <EditorFrame
      editorClass="pipeline"
      header={headerZone}
      zones={editorZones}
      contentMode="pass-through"
    >
      {!view.pipelineId && !view.startCommandId && !view.chain ? (
        <EditorFrame.Empty>{t('editor.pipelineNoRun')}</EditorFrame.Empty>
      ) : (
        <div ref={rootRef} className="ze-pipeline ze-scroll-fade-y">
          {/* 全量显示开关:legacy 视图精简↔全量;chain 模式改由下方「展开详情」托管,此处不渲。 */}
          {!view.chain && displayHidesSomething(view.display) ? (
            <div className="ze-pipeline__meta">
              <button
                type="button" className="ze-button" data-size="sm" data-variant="ghost"
                aria-pressed={showAll}
                onClick={() => setShowAll((v) => !v)}
              >{showAll ? t('editor.pipelineShowLess') : t('editor.pipelineShowAll')}</button>
            </div>
          ) : null}
          {chainView ? (
            <>
              {chainView}
              {/* 展开详情 notebook:复用 showAll 状态(展开=完整 stage / 日志 / 导出 / StartConsole)。 */}
              <div className="ze-pipeline__details-toggle">
                <button
                  type="button" className="ze-button" data-size="sm" data-variant="ghost"
                  aria-expanded={showAll}
                  onClick={() => setShowAll((v) => !v)}
                >{showAll ? t('editor.pipelineShowLess') : t('editor.pipelineShowAll')}</button>
              </div>
              {showAll ? <div className="ze-pipeline__details">{notebookBody}</div> : null}
            </>
          ) : mergedMode ? mergedCells : notebookBody}
        </div>
      )}
    </EditorFrame>
  )
}
