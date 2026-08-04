/**
 * actionGating — FormAction 动作置灰判定 + 共享钮渲染(供 ActionBarZone / InfoControlUnit + vitest)
 *
 * 轨①前置可见:requiresFields 声明的非空字段在 active 实体里为空 → 该动作置灰并
 * 给出缺失字段名(label 走 schema.properties[field].label,语言中立回退裸 key)。
 * 后端 execute 仍兜底校验(轨②)。
 * renderFormActionButton:把 ActionBarZone 的钮渲染(confirm 守卫 / busy 单飞 / requiresFields
 * 置灰 / 与运行目标冲突的动作置灰)抽出共享给单元控制区;busy 单飞 state 由各控制区自持。
 */

import type { ReactNode } from 'react'
import type {
  DispatchFn, EntityDict, EntityTypeSchema, FormActionResultConsumer,
} from '../types'
import type { FormAction, InspectorControlSpec, InspectorSectionSpec } from './types'
import { AiRouteGate } from '../../AiRouteGate'
import type { AiRoutesState } from '../../../03-authentication/aiRoutes'
import { AiConsentGate } from '../../AiConsentGate'
import type { AiConsentState } from '../../../03-authentication/aiConsent'

export function isEmptyValue(v: unknown): boolean {
  return v === undefined || v === null || (typeof v === 'string' && v.trim() === '')
}

/** view_state.actions / actionsByType[type] → FormAction[](解析守卫,坏声明丢弃;
 *  parseGalleryActions 同款先例)。空数组/非数组 → undefined。 */
export function parseFormActions(v: unknown): FormAction[] | undefined {
  if (!Array.isArray(v)) return undefined
  const out = v.filter((x): x is FormAction => {
    if (!x || typeof x !== 'object') return false
    const a = x as FormAction
    // 文案二选一:labelKey(可译)或 label(语言中立直显);至少一个为字符串。
    if (typeof a.command !== 'string') return false
    if (typeof a.labelKey !== 'string' && typeof a.label !== 'string') return false
    if (a.scope !== undefined && a.scope !== 'single' && a.scope !== 'selection') return false
    if (a.result !== undefined) {
      if (!a.result || typeof a.result !== 'object' || Array.isArray(a.result)) return false
      if (Object.keys(a.result).length !== 1 || a.result.kind !== 'download') return false
      // dialog 组件拥有自己的 dispatch 生命周期，当前没有共享结果回传缝；拒绝声明而非静默失效。
      if (a.dialog !== undefined) return false
    }
    return true
  })
  return out.length ? out : undefined
}

/** 把一次 dispatch 的原始结果恰好转交一次给装配层。成功/提交事实由 App 统一判定，
 * core action renderer 不解释 transport wire，也不认识任何业务 command id。 */
export async function forwardFormActionResult(
  action: FormAction,
  result: unknown,
  consume?: FormActionResultConsumer,
): Promise<void> {
  if (!action.result || result === undefined || !consume) return
  await consume(action.result, result)
}

/** 动作显示文案:labelKey(走 t())优先,回退 label(语言中立),都无则空串。 */
export function resolveActionLabel(a: { labelKey?: string; label?: string }, t: (key: string) => string): string {
  return a.labelKey ? t(a.labelKey) : (a.label ?? '')
}

/** 确认文案:confirmKey(走 t())优先,回退 confirm 字符串;布尔 confirm=true → 无专属文案(回退动作名)守卫;无确认 → undefined。 */
export function resolveActionConfirm(
  a: { confirmKey?: string; confirm?: string | boolean },
  t: (key: string) => string,
  fallbackLabel: string,
): string | undefined {
  if (a.confirmKey) return t(a.confirmKey)
  if (typeof a.confirm === 'string') return a.confirm
  if (a.confirm === true) return fallbackLabel
  return undefined
}

/** view_state.sectionsByType[type] / view_state.sections → InspectorSectionSpec[]
 *  (解析守卫,坏声明丢弃;parseFormActions/parseGalleryActions 同款)。非数组/空 → undefined。
 *  id/titleKey 必填字符串、kind 闭集;actions 经 parseFormActions 过滤;fields/paramSchema 透传。 */
export function parseInspectorSections(v: unknown): InspectorSectionSpec[] | undefined {
  if (!Array.isArray(v)) return undefined
  const KINDS = new Set(['fields', 'readonly-kv', 'params', 'controls', 'preview', 'variants', 'asset'])
  const CONTROL_TYPES = new Set(['text', 'number', 'integer', 'boolean', 'select', 'color'])
  const entries = v
    .filter((x): x is Record<string, unknown> => !!x && typeof x === 'object')
    .filter((x) => typeof x.id === 'string' && x.id.trim().length > 0 && typeof x.titleKey === 'string' && KINDS.has(x.kind as string))
  // Duplicate ids have no collision-free meaning: render order/title are explicitly forbidden as
  // identity fallbacks. Reject the declaration instead of coupling two sections to one preference.
  if (new Set(entries.map((x) => x.id)).size !== entries.length) return undefined
  const out = entries
    .map((x) => {
      const sec: InspectorSectionSpec = {
        id: x.id as string,
        titleKey: x.titleKey as string,
        kind: x.kind as InspectorSectionSpec['kind'],
      }
      if (typeof x.title === 'string') sec.title = x.title
      if (typeof x.defaultOpen === 'boolean') sec.defaultOpen = x.defaultOpen
      if (typeof x.collapsible === 'boolean') sec.collapsible = x.collapsible
      if (Array.isArray(x.fields)) sec.fields = x.fields.filter((f): f is string => typeof f === 'string')
      if (Array.isArray(x.paramSchema)) sec.paramSchema = x.paramSchema as InspectorSectionSpec['paramSchema']
      if (Array.isArray(x.controls)) {
        const controls = x.controls
          .filter((c): c is Record<string, unknown> => !!c && typeof c === 'object')
          .filter((c) => typeof c.key === 'string' && typeof c.path === 'string' && CONTROL_TYPES.has(c.type as string))
          .map((c): InspectorControlSpec => ({
            key: c.key as string,
            path: c.path as string,
            type: c.type as InspectorControlSpec['type'],
            ...(typeof c.labelKey === 'string' ? { labelKey: c.labelKey } : {}),
            ...(typeof c.label === 'string' ? { label: c.label } : {}),
            ...(Array.isArray(c.options) ? { options: c.options.filter((o) => typeof o === 'string' || (!!o && typeof o === 'object' && typeof (o as { value?: unknown }).value === 'string')) as never } : {}),
            ...(typeof c.min === 'number' ? { min: c.min } : {}),
            ...(typeof c.max === 'number' ? { max: c.max } : {}),
            ...(typeof c.step === 'number' ? { step: c.step } : {}),
            ...(typeof c.command === 'string' ? { command: c.command } : {}),
            ...(c.commandArgs && typeof c.commandArgs === 'object' && !Array.isArray(c.commandArgs) ? { commandArgs: c.commandArgs as Record<string, unknown> } : {}),
            ...(typeof c.valueArg === 'string' ? { valueArg: c.valueArg } : {}),
            ...(typeof c.targetArg === 'string' ? { targetArg: c.targetArg } : {}),
            ...(typeof c.pathArg === 'string' ? { pathArg: c.pathArg } : {}),
            ...(typeof c.disabled === 'boolean' ? { disabled: c.disabled } : {}),
            ...(typeof c.conflictsWithRun === 'boolean' ? { conflictsWithRun: c.conflictsWithRun } : {}),
          }))
        if (controls.length) sec.controls = controls as InspectorSectionSpec['controls']
      }
      if (typeof x.previewField === 'string') sec.previewField = x.previewField   // kind==='preview':要预览的 asset_ref 字段
      if (typeof x.assetField === 'string') sec.assetField = x.assetField           // kind==='asset':可挂载的 asset_ref 字段
      if (typeof x.accept === 'string') sec.accept = x.accept                       // kind==='asset':接受格式(file accept)
      if (typeof x.multiple === 'boolean') sec.multiple = x.multiple                // kind==='asset':强制多资产模式
      // kind==='variants':副本/候选选择条(闭集字段名 + 命令 id)
      if (typeof x.childType === 'string') sec.childType = x.childType
      if (typeof x.childParentField === 'string') sec.childParentField = x.childParentField
      if (typeof x.childThumbField === 'string') sec.childThumbField = x.childThumbField
      if (typeof x.childScoreField === 'string') sec.childScoreField = x.childScoreField
      if (typeof x.adoptedField === 'string') sec.adoptedField = x.adoptedField
      if (typeof x.adoptCommand === 'string') sec.adoptCommand = x.adoptCommand
      if (typeof x.childDeleteCommand === 'string') sec.childDeleteCommand = x.childDeleteCommand
      const acts = parseFormActions(x.actions)
      if (acts) sec.actions = acts
      return sec
    })
  return out.length ? out : undefined
}

/** 该动作在当前实体数据下缺失的前置字段(显示名列表;空数组=可用)。 */
export function missingFields(
  action: FormAction,
  entityData: Record<string, unknown>,
  schema?: EntityTypeSchema,
): string[] {
  return (action.requiresFields ?? [])
    .filter((f) => isEmptyValue(entityData[f]))
    .map((f) => (schema?.properties?.[f] as { label?: string } | undefined)?.label ?? f)
}

/** 字段值显隐条件:缺省可见;字符串全等,数组为允许集合。 */
export function actionVisible(action: FormAction, entityData: Record<string, unknown>): boolean {
  const cond = action.visibleWhen
  if (!cond || typeof cond !== 'object') return true
  for (const [field, expected] of Object.entries(cond)) {
    const actual = String(entityData[field] ?? '')
    if (Array.isArray(expected)) {
      if (!expected.includes(actual)) return false
    } else if (actual !== expected) {
      return false
    }
  }
  return true
}

/**
 * 活动 run 期间只禁会破坏/替换目标的动作。声明可显式覆盖 danger 的视觉缺省；内置
 * entity.delete 永远冲突。普通动作不再被整条 actionbar 连坐。
 */
export function actionConflictsWithRun(action: FormAction): boolean {
  if (action.command === 'entity.delete') return true
  if (typeof action.conflictsWithRun === 'boolean') return action.conflictsWithRun
  return action.variant === 'danger'
}

/** scope 派发参数:single → {type,id,expected_version}(带 CAS 版本让内置 entity.delete 等
 *  可直接当动作用;mod 业务命令忽略多余键,与旧 SlotFormActions 的 {type,id} 契约兼容);
 *  selection → {type,ids}(无多选退化为 [activeId];批量命令自带版本语义)。 */
export function actionArgs(
  action: FormAction,
  entityType: string,
  activeId: string,
  selectionIds?: string[],
  activeVersion?: number,
): Record<string, unknown> {
  if (action.scope === 'selection') {
    const ids = selectionIds && selectionIds.length > 0 ? selectionIds : [activeId]
    return { type: entityType, ids }
  }
  return activeVersion === undefined
    ? { type: entityType, id: activeId }
    : { type: entityType, id: activeId, expected_version: activeVersion }
}

/** FormAction 钮的共享渲染(ActionBarZone 钮的提取;ActionBarZone / InfoControlUnit 控制区复用)。
 *  固定行为:confirm 守卫 + busy 单飞(任一执行中全组禁点)+ requiresFields 置灰 + tooltip +
 *  running(实体执行中)只置灰 conflictsWithRun/danger 动作。busy/setBusy 由调用方持有。
 *  requireFieldsLabel:requiresFields tooltip 的 i18n 文案器(t('slot.requiresFields',{fields}))。 */
export function renderFormActionButton(opts: {
  action: FormAction
  entityType: string
  entity: EntityDict
  selectionIds?: string[]
  schema?: EntityTypeSchema
  running?: boolean
  dispatch?: DispatchFn
  consumeActionResult?: FormActionResultConsumer
  busy: string | null
  setBusy: (command: string | null) => void
  requireFieldsLabel: (fields: string) => string
  /** chrome 文案解析器(labelKey/confirmKey → 文案);ActionBarZone/InfoControlUnit 经 useT() 传入。 */
  t: (key: string) => string
  /** action.dialog 设了时,点击改为打开已注册弹窗(开关态由调用方持有);不传则 dialog 动作退化为直发。 */
  onOpenDialog?: (action: FormAction) => void
  /** confirm 守卫改走样式化弹窗:调用方持有「待确认」态、渲染 ConfirmDialog,确认时回调 run。不传则回退裸 window.confirm。 */
  onRequestConfirm?: (text: string, run: () => void) => void
  /** 本动作命令声明的 AI 通道(command.requires_route);='image' 且 aiRoutes 未绑时按钮门控。可选、加性。 */
  requiresRoute?: string
  /** AI image 路线态(装配层经 SlotContext 透传);未提供=不门控(驱动层 428 兜底)。 */
  aiRoutes?: AiRoutesState
  /** AI 内容合规同意态(装配层经 SlotContext 透传);requiresRoute 非空且未接受当前条款时拦首击弹条款。可选、加性。 */
  aiConsent?: AiConsentState
}): ReactNode {
  const { action: a, entityType, entity, selectionIds, schema, running, dispatch, consumeActionResult, busy, setBusy, requireFieldsLabel, t, onOpenDialog, onRequestConfirm, requiresRoute, aiRoutes, aiConsent } = opts
  if (!actionVisible(a, entity.data ?? {})) return null
  // 缺少框架 consumer 或 dialog 绕开共享 dispatch 时不展示结果动作，避免生成“能点但拿不到”的死按钮。
  if (a.result && (!consumeActionResult || a.dialog)) return null
  const missing = missingFields(a, entity.data ?? {}, schema)
  const blockedReason = missing.length > 0 ? requireFieldsLabel(missing.join(', ')) : undefined
  const label = resolveActionLabel(a, t)
  const confirmText = resolveActionConfirm(a, t, label)
  const btn = (
    <button
      key={a.command}
      type="button"
      className="ze-button"
      data-size="sm"
      data-variant={a.variant ?? 'primary'}
      data-ze-cmd={a.command}
      aria-label={label}
      disabled={(running === true && actionConflictsWithRun(a)) || busy !== null || blockedReason !== undefined}
      title={blockedReason}
      onClick={() => {
        // dialog 动作:开弹窗收集输入,弹窗自管确认与 dispatch(忽略 confirm/直发)。
        if (a.dialog && onOpenDialog) { onOpenDialog(a); return }
        const run = async () => {
          setBusy(a.command)
          try {
            const result = await Promise.resolve(dispatch?.(
              a.command,
              actionArgs(a, entityType, entity.id, selectionIds, entity.version),
            ))
            await forwardFormActionResult(a, result, consumeActionResult)
          } finally {
            setBusy(null)
          }
        }
        // confirm 守卫:优先样式化弹窗(onRequestConfirm),回退裸 window.confirm。
        if (confirmText !== undefined) {
          if (onRequestConfirm) { onRequestConfirm(confirmText, () => { void run() }); return }
          if (!window.confirm(confirmText)) return
        }
        void run()
      }}
    >
      {busy === a.command ? <span className="ze-spinner" data-size="sm" aria-hidden="true" /> : label}
    </button>
  )
  // 3.56/3.65 上生到 form action:requires_route='image' 且未绑 → AiRouteGate 置灰+去绑定;
  // 任何 requiresRoute 且未接受当前条款 → AiConsentGate 拦首击弹条款。条款门在外、路线门在内;
  // 未声明/无 state/条款未配时逐层透明透传(加性零影响)。key 落最外层元素。
  let node = btn
  if (requiresRoute === 'image' && aiRoutes) {
    node = <AiRouteGate key={a.command} state={aiRoutes}>{node}</AiRouteGate>
  }
  if (requiresRoute && aiConsent) {
    node = <AiConsentGate key={a.command} state={aiConsent}>{node}</AiConsentGate>
  }
  return node
}
