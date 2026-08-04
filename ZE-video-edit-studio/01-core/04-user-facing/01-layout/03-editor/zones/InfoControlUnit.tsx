/**
 * InfoControlUnit — 信息+控制单元(v2 编辑器功能区核心砖块)
 *
 * 一个单元 = 固定二分:`.ze-editor__unit-info`[data-role=info](信息区:展示/编辑数据)+
 * `.ze-editor__unit-control`[data-role=control](控制区:本单元的命令钮组)。
 * **铁律**:信息区不夹命令、控制区收一处、二分边界清晰(控制区 border-top 发丝线);嵌套限一层
 * (单元内不再嵌单元)。可选标题(collapsible→.ze-disclosure 折叠信息区)。
 *
 * 信息区按 kind 渲染(InspectorSectionSpec 闭集,无裸 JSX):
 *   - fields     → 该字段子集逐 FieldRow(可编辑,受控 draft/commit;复用 FormZone 写回语义)
 *   - readonly-kv→ 只读 InfoField(.ze-kv-list)
 *   - params     → ParamPanel(schema 驱动控件栈)
 *   - preview    → 把某 asset_ref 字段(previewField)渲为只读图片预览(经 assetUrl 解析;位置示意/修饰图用)
 * 控制区:FormAction 钮组(复用 renderFormActionButton;busy 单飞域为本单元独立)。
 */

import { useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useT, useLocale } from '@ze/core/04-user-facing/05-preferences'
import { SlotContext } from '../../lib/SlotContext'
import { Collapsible, InfoField } from '../primitives/InspectorSection'
import { ParamPanel } from '../primitives/ParamPanel'
import { ThumbnailGrid, type ThumbItem } from '../primitives/ThumbnailGrid'
import { AssetRefInput } from '../primitives/AssetRefInput'
import { FieldRow } from './FieldRow'
import type { AiRoutesState } from '../../../03-authentication/aiRoutes'
import { i18nDataField, i18nLabel } from '../lib/fieldSchema'
import { entityRunIsActive, isVacantPendingEntity } from '../lib/entityRunPresentation'
import { renderFormActionButton } from './actionGating'
import { resolveDialog } from './dialogRegistry'
import { ConfirmDialog } from './ConfirmDialog'
import type {
  AssetRefDict, AssetUrlFn, DispatchFn, EntityDict, EntityRefOptions, EntityRunStatus,
  EntityTypeSchema, FieldSchema, FormActionResultConsumer,
} from '../types'
import type { FormAction, InspectorControlSpec, InspectorSectionSpec } from './types'

export interface InfoControlUnitProps {
  section: InspectorSectionSpec
  entity: EntityDict
  schema?: EntityTypeSchema
  status?: EntityRunStatus
  /** scope:'selection' 动作的多选派发集(无则退化 [entity.id])。 */
  selectionIds?: string[]
  /** kind==='preview'|'variants'|'asset' 解析 asset_ref → URL(经 SlotContext 透传)。 */
  assetUrl?: AssetUrlFn
  /** kind==='asset' 上传缝:bytes → asset_ref(经 SlotContext 透传;缺省则该单元降级只读)。 */
  uploadAsset?: (bytes: Uint8Array, mime: string, ext?: string) => Promise<AssetRefDict>
  /** kind==='variants':本实体的副本/候选子实体集(装配层按 childType+childParentField 过滤后注入)。 */
  childEntities?: EntityDict[]
  /** entity_ref 字段候选表(按绑定类型分桶;经 fields kind 的 FieldRow 透传)。 */
  entityRefOptions?: EntityRefOptions
  dispatch?: DispatchFn
  consumeActionResult?: FormActionResultConsumer
}

/** 信息区:只读图片预览(preview kind)。previewField 指向的 asset_ref 经 assetUrl 解析为 <img>。 */
function PreviewInfo({
  field, entity, assetUrl, vacantPending, runActive,
}: {
  field?: string; entity: EntityDict; assetUrl?: AssetUrlFn
  vacantPending?: boolean; runActive?: boolean
}): ReactNode {
  const t = useT()
  const raw = field ? entity.data[field] : undefined
  const ref = asAssetRefDict(raw) ?? asAssetRefList(raw)[0] ?? null
  const src = ref && assetUrl ? assetUrl(ref) : undefined
  return (
    <div className="ze-editor__unit-preview">
      {vacantPending ? <span className="ze-spinner" data-size="sm" />
        : src ? <img src={src} alt="" loading="lazy" />
        : <div className="ze-editor__empty">{t('editor.noAssetRef')}</div>}
      {runActive && !vacantPending ? (
        <span className="ze-editor__unit-preview-run"><span className="ze-spinner" data-size="sm" /></span>
      ) : null}
    </div>
  )
}

/** asset_ref dict(含 sha256)解析守卫;否则 null。 */
function asAssetRefDict(v: unknown): AssetRefDict | null {
  return v && typeof v === 'object' && 'sha256' in (v as Record<string, unknown>) ? (v as AssetRefDict) : null
}

/** asset_ref 或 asset_ref[] 兼容读取;旧单图数据在多图 schema 下按一张图显示。 */
function asAssetRefList(v: unknown): AssetRefDict[] {
  if (Array.isArray(v)) return v.map(asAssetRefDict).filter((r): r is AssetRefDict => !!r)
  const one = asAssetRefDict(v)
  return one ? [one] : []
}

function isAssetRefArraySchema(fs: FieldSchema | undefined): boolean {
  if (!fs || fs.type !== 'array') return false
  const items = fs.items as FieldSchema | undefined
  return fs.format === 'asset' || items?.type === 'asset_ref' || items?.format === 'asset'
}

function keyOfAssetValue(v: unknown): string {
  return asAssetRefList(v).map((ref) => ref.sha256).join('\n')
}

type QueuedAssetUpdate = {
  hasDesired: boolean
  desired: unknown
  inFlight: boolean
  sentKey?: string
  sentVersion?: number
}

function useQueuedAssetCommit(entity: EntityDict, dispatch?: DispatchFn): (field: string, value: unknown) => void {
  const latestRef = useRef<{ entity: EntityDict; dispatch?: DispatchFn }>({ entity, dispatch })
  const queueRef = useRef<Map<string, QueuedAssetUpdate>>(new Map())
  const flushFieldRef = useRef<(field: string) => void>(() => {})

  const flushField = useCallback((field: string) => {
    const rec = queueRef.current.get(field)
    const latest = latestRef.current
    if (!rec || !rec.hasDesired || rec.inFlight || !latest.dispatch) return
    const desired = rec.desired
    const desiredKey = keyOfAssetValue(desired)
    if (keyOfAssetValue(latest.entity.data[field]) === desiredKey) {
      rec.hasDesired = false
      if (!rec.inFlight) queueRef.current.delete(field)
      return
    }
    rec.inFlight = true
    rec.sentKey = desiredKey
    rec.sentVersion = latest.entity.version
    void Promise.resolve(latest.dispatch('entity.update', {
      type: latest.entity.type,
      id: latest.entity.id,
      patches: { [field]: desired },
      expected_version: latest.entity.version,
    })).finally(() => {
      globalThis.setTimeout(() => {
        const current = queueRef.current.get(field)
        if (!current?.inFlight || current.sentVersion !== latestRef.current.entity.version) return
        current.inFlight = false
        flushFieldRef.current(field)
      }, 8000)
    })
  }, [])
  flushFieldRef.current = flushField

  useEffect(() => {
    latestRef.current = { entity, dispatch }
    for (const [field, rec] of queueRef.current.entries()) {
      const currentKey = keyOfAssetValue(entity.data[field])
      if (rec.inFlight && (rec.sentVersion !== entity.version || currentKey === rec.sentKey)) {
        rec.inFlight = false
        if (rec.hasDesired && currentKey === keyOfAssetValue(rec.desired)) rec.hasDesired = false
      }
      if (!rec.inFlight && !rec.hasDesired) queueRef.current.delete(field)
      else flushField(field)
    }
  }, [entity, dispatch, flushField])

  return useCallback((field: string, value: unknown) => {
    const rec = queueRef.current.get(field) ?? { hasDesired: false, desired: undefined, inFlight: false }
    rec.desired = value
    rec.hasDesired = true
    queueRef.current.set(field, rec)
    flushFieldRef.current(field)
  }, [])
}

/** 信息区:副本/候选选择条(variants kind)。把当前实体的子实体(childEntities)渲为缩略图,
 *  点一张 = 采纳(adoptCommand),高亮当前已采纳(adoptedField);可选删除(childDeleteCommand)。
 *  复用 ThumbnailGrid primitive —— 不引 GalleryEditor(editor 不 import editor)。 */
function VariantsInfo({
  section, entity, childEntities, assetUrl, runActive, dispatch,
}: {
  section: InspectorSectionSpec; entity: EntityDict
  childEntities?: EntityDict[]; assetUrl?: AssetUrlFn; runActive?: boolean; dispatch?: DispatchFn
}): ReactNode {
  const t = useT()
  const childType = section.childType
  const thumbField = section.childThumbField ?? 'imageRef'
  const adoptedId = section.adoptedField
    ? (typeof entity.data[section.adoptedField] === 'string' ? (entity.data[section.adoptedField] as string) : null)
    : null
  const items: ThumbItem[] = (childEntities ?? []).map((c) => {
    const ref = asAssetRefDict(c.data[thumbField])
    const scoreRaw = section.childScoreField ? c.data[section.childScoreField] : undefined
    return {
      id: c.id,
      src: ref && assetUrl ? assetUrl(ref) : undefined,
      label: c.name || undefined,
      score: typeof scoreRaw === 'number' ? scoreRaw : undefined,
    }
  })
  // 点一张=采纳(onSelect),与 v1「click a thumbnail to adopt」一致;selectedId=已采纳卡(高亮)。
  const onSelect = !runActive && section.adoptCommand && childType
    ? (id: string) => dispatch?.(section.adoptCommand!, { type: childType, id })
    : undefined
  // 删一张:派发单实体 {type,id,expected_version}(内置 entity.delete 只收单实体;批量 {type,ids} 会崩)。
  const onDelete = !runActive && section.childDeleteCommand && childType
    ? (id: string) => {
        const c = (childEntities ?? []).find((e) => e.id === id)
        dispatch?.(section.childDeleteCommand!, { type: childType, id, expected_version: c?.version })
      }
    : undefined
  // 直接渲 ThumbnailGrid(.ze-thumb-grid 已定义,@container 响应);随 form-body 滚动,不另包未定义容器。
  return (
    <ThumbnailGrid
      items={items}
      selectedId={adoptedId}
      onSelect={onSelect}
      onDelete={onDelete}
      layoutMode="grid"
      thumbSize="sm"
      emptyText={t('editor.noContent')}
    />
  )
}

/** 信息区:可编辑字段子集(fields kind)。受控 draft/jsonText/invalid,onBlur 提交 entity.update。 */
function FieldsInfo({
  fields, entity, schema, status, dispatch, entityRefOptions, aiRoutes,
}: {
  fields: string[]; entity: EntityDict; schema?: EntityTypeSchema
  status?: EntityRunStatus; dispatch?: DispatchFn; entityRefOptions?: EntityRefOptions
  aiRoutes?: AiRoutesState
}): ReactNode {
  const [draft, setDraft] = useState<Record<string, unknown>>(entity.data)
  const [jsonText, setJsonText] = useState<Record<string, string>>({})
  const [invalid, setInvalid] = useState<Record<string, boolean>>({})
  const { locale } = useLocale()

  // entity 换了(不同 id 或外部更新 bump version)→ 重置草稿(对齐 FormZone 重置律)。
  useEffect(() => {
    setDraft(entity.data)
    setJsonText({})
    setInvalid({})
  }, [entity.id, entity.version, entity.data])

  const phase = status?.phase
  const loading = isVacantPendingEntity(entity, status)
  const readOnly = entity.read_only

  const commit = (field: string, value: unknown) => {
    if (value === entity.data[field]) return
    // 返回 dispatch 结果:AssetRefInput 的连续粘贴/多图写入队列需 await 上一次提交,防后写覆盖前写。
    return dispatch?.('entity.update', {
      type: entity.type, id: entity.id,
      patches: { [field]: value },
      expected_version: entity.version,
    })
  }

  const entries: [string, FieldSchema][] = fields.map((f) => [f, (schema?.properties?.[f] ?? {}) as FieldSchema])
  return (
    <>
      {entries.map(([field, fs]) => {
        const required = fs.required === true || (schema?.required?.includes(field) ?? false)
        // 按 UI 语言把「取值/提交」绑到对应语言的伴生字段(x-ze-i18n-field);标签仍走概念字段 fs。
        const df = i18nDataField(fs, field, locale)
        return (
          <FieldRow
            key={field}
            field={field} fs={fs}
            baseValue={entity.data[df]} value={draft[df]}
            required={required} readOnly={readOnly}
            loading={loading} phase={phase}
            jsonText={df in jsonText ? jsonText[df] : undefined}
            jsonInvalid={invalid[df] === true}
            onDraft={(value) => setDraft({ ...draft, [df]: value })}
            onCommit={(value) => commit(df, value)}
            onJsonText={(text) => setJsonText({ ...jsonText, [df]: text })}
            onJsonInvalid={(inv) => setInvalid({ ...invalid, [df]: inv })}
            entityRefOptions={entityRefOptions}
            siblingData={draft}
            aiRoutes={aiRoutes}
          />
        )
      })}
    </>
  )
}

/** 信息区:只读键值(readonly-kv kind)。label/value 均按 UI 语言解析(i18nLabel 出标签、i18nDataField 换取值字段)。 */
function ReadonlyKvInfo({
  fields, entity, schema,
}: { fields: string[]; entity: EntityDict; schema?: EntityTypeSchema }): ReactNode {
  const { locale } = useLocale()
  return (
    <div className="ze-kv-list">
      {fields.map((f) => {
        const fs = (schema?.properties?.[f] ?? {}) as FieldSchema
        const v = entity.data[i18nDataField(fs, f, locale)]
        const display = v === null || v === undefined ? ''
          : typeof v === 'object' ? JSON.stringify(v)
          : String(v)
        return <InfoField key={f} label={i18nLabel(fs, f, locale)} value={display} />
      })}
    </div>
  )
}

function normalizeControlPath(entity: EntityDict, path: string): string[] {
  const prefix = `${entity.type}.`
  const rel = path.startsWith(prefix) ? path.slice(prefix.length)
    : path.startsWith('data.') ? path.slice(5)
    : path
  return rel.split('.').filter(Boolean)
}

function isIndex(part: string): boolean {
  return /^\d+$/.test(part)
}

function getByPath(source: unknown, parts: string[]): unknown {
  let cur = source as unknown
  for (const part of parts) {
    if (cur === null || cur === undefined) return undefined
    if (Array.isArray(cur) && isIndex(part)) cur = cur[Number(part)]
    else if (typeof cur === 'object') cur = (cur as Record<string, unknown>)[part]
    else return undefined
  }
  return cur
}

function cloneContainer(current: unknown, nextPart?: string): Record<string, unknown> | unknown[] {
  if (Array.isArray(current)) return [...current]
  if (current && typeof current === 'object') return { ...(current as Record<string, unknown>) }
  return nextPart && isIndex(nextPart) ? [] : {}
}

function setDeep(current: unknown, parts: string[], value: unknown): unknown {
  if (!parts.length) return value
  const [head, ...rest] = parts
  const base = cloneContainer(current, rest[0])
  if (Array.isArray(base) && isIndex(head)) {
    base[Number(head)] = setDeep(base[Number(head)], rest, value)
    return base
  }
  const record = base as Record<string, unknown>
  record[head] = setDeep(record[head], rest, value)
  return record
}

function patchForPath(data: Record<string, unknown>, parts: string[], value: unknown): Record<string, unknown> {
  if (!parts.length) return {}
  const [head, ...rest] = parts
  return { [head]: setDeep(data[head], rest, value) }
}

function optionValue(option: NonNullable<InspectorControlSpec['options']>[number]): string {
  return typeof option === 'string' ? option : option.value
}

function optionLabel(option: NonNullable<InspectorControlSpec['options']>[number], t: (key: string) => string): string {
  if (typeof option === 'string') return option
  if (option.labelKey) return t(option.labelKey)
  return option.label ?? option.value
}

function valueForInput(value: unknown): string {
  return value === null || value === undefined ? '' : String(value)
}

const FALLBACK_COLOR = `${String.fromCharCode(35)}000000`

function coerceControlValue(control: InspectorControlSpec, raw: string | boolean): unknown {
  if (control.type === 'boolean') return Boolean(raw)
  if (control.type === 'number' || control.type === 'integer') {
    const n = typeof raw === 'number' ? raw : Number(raw)
    if (!Number.isFinite(n)) return null
    return control.type === 'integer' ? Math.round(n) : n
  }
  return String(raw)
}

function ControlRow({
  control, entity, runActive, dispatch,
}: {
  control: InspectorControlSpec; entity: EntityDict; runActive?: boolean; dispatch?: DispatchFn
}): ReactNode {
  const t = useT()
  const parts = normalizeControlPath(entity, control.path)
  const value = getByPath(entity.data, parts)
  const [draft, setDraft] = useState(valueForInput(value))
  useEffect(() => {
    setDraft(valueForInput(value))
  }, [entity.id, entity.version, control.path, value])
  const label = control.labelKey ? t(control.labelKey) : control.label ?? control.key
  const readOnly = entity.read_only || control.disabled || (runActive && control.conflictsWithRun === true)

  const commit = (next: unknown) => {
    if (control.command) {
      const payload: Record<string, unknown> = {
        ...(control.commandArgs ?? {}),
        type: entity.type,
        expected_version: entity.version,
      }
      payload[control.targetArg ?? 'id'] = entity.id
      if (control.pathArg !== '') payload[control.pathArg ?? 'path'] = parts.join('.')
      payload[control.valueArg ?? 'value'] = next
      dispatch?.(control.command, payload)
      return
    }
    const patches = patchForPath(entity.data, parts, next)
    if (!Object.keys(patches).length) return
    dispatch?.('entity.update', {
      type: entity.type, id: entity.id,
      patches, expected_version: entity.version,
    })
  }

  if (control.type === 'boolean') {
    return (
      <label className="ze-field-row" data-density="compact">
        <span className="ze-field-row__label">{label}</span>
        <input
          type="checkbox" className="ze-checkbox" checked={Boolean(value)}
          disabled={readOnly}
          onChange={(e) => commit(e.currentTarget.checked)}
        />
      </label>
    )
  }
  if (control.type === 'select') {
    return (
      <label className="ze-field-row" data-density="compact">
        <span className="ze-field-row__label">{label}</span>
        <span className="ze-select" data-size="sm">
          <select
            className="ze-select__control"
            value={valueForInput(value)}
            disabled={readOnly}
            onChange={(e) => commit(e.currentTarget.value)}
          >
            {(control.options ?? []).map((option) => (
              <option key={optionValue(option)} value={optionValue(option)}>{optionLabel(option, t)}</option>
            ))}
          </select>
        </span>
      </label>
    )
  }
  if (control.type === 'color') {
    return (
      <label className="ze-field-row" data-density="compact" data-control="color">
        <span className="ze-field-row__label">{label}</span>
        <input
          type="color" className="ze-field-row__color"
          value={/^#[0-9a-fA-F]{6}$/.test(draft) ? draft : FALLBACK_COLOR}
          disabled={readOnly}
          onChange={(e) => { setDraft(e.currentTarget.value); commit(e.currentTarget.value) }}
        />
      </label>
    )
  }
  const numeric = control.type === 'number' || control.type === 'integer'
  return (
    <label className="ze-field-row" data-density="compact">
      <span className="ze-field-row__label">{label}</span>
      <input
        type={numeric ? 'number' : 'text'}
        className="ze-input"
        data-size="sm"
        min={numeric ? control.min : undefined}
        max={numeric ? control.max : undefined}
        step={numeric ? control.step : undefined}
        value={draft}
        disabled={readOnly}
        onChange={(e) => setDraft(e.currentTarget.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') commit(coerceControlValue(control, draft)) }}
        onBlur={() => commit(coerceControlValue(control, draft))}
      />
    </label>
  )
}

function ControlsInfo({
  controls, entity, runActive, dispatch,
}: {
  controls: InspectorControlSpec[]; entity: EntityDict; runActive?: boolean; dispatch?: DispatchFn
}): ReactNode {
  return (
    <>
      {controls.map((control) => (
        <ControlRow key={control.key} control={control} entity={entity} runActive={runActive} dispatch={dispatch} />
      ))}
    </>
  )
}

export function InfoControlUnit({
  section, entity, schema, status, selectionIds, assetUrl, uploadAsset, childEntities, entityRefOptions,
  dispatch, consumeActionResult,
}: InfoControlUnitProps): ReactNode {
  const t = useT()
  // 安全读上下文(无 provider 展示台返 null 不门控=字节级不变);requires_route='image' 动作据此门控。
  const slotCtx = useContext(SlotContext)
  const [busy, setBusy] = useState<string | null>(null)
  const [dialogAction, setDialogAction] = useState<FormAction | null>(null)
  const [confirmReq, setConfirmReq] = useState<{ text: string; run: () => void } | null>(null)
  const phase = status?.phase
  const runActive = entityRunIsActive(status)
  const vacantPending = isVacantPendingEntity(entity, status)
  const title = section.title ?? t(section.titleKey)
  const DialogComp = resolveDialog(dialogAction?.dialog)
  const commitAsset = useQueuedAssetCommit(entity, dispatch)

  const info = useMemo<ReactNode>(() => {
    const fields = section.fields ?? []
    if (section.kind === 'variants') {
      return <VariantsInfo section={section} entity={entity} childEntities={childEntities} assetUrl={assetUrl} runActive={runActive} dispatch={dispatch} />
    }
    if (section.kind === 'params') {
      const paramSchema = section.paramSchema ?? []
      const paramProps: Record<string, FieldSchema> = {}
      for (const fs of paramSchema) {
        const name = (fs as { name?: string }).name
        if (typeof name === 'string') paramProps[name] = fs
      }
      return (
        <ParamPanel
          schema={{ type: 'object', properties: paramProps }}
          values={entity.data}
          readOnly={entity.read_only}
          status={vacantPending ? 'loading' : undefined}
          onCommit={(field, value) => {
            if (value === entity.data[field]) return
            dispatch?.('entity.update', {
              type: entity.type, id: entity.id,
              patches: { [field]: value }, expected_version: entity.version,
            })
          }}
        />
      )
    }
    if (section.kind === 'controls') {
      return <ControlsInfo controls={section.controls ?? []} entity={entity} runActive={runActive} dispatch={dispatch} />
    }
    if (section.kind === 'readonly-kv') {
      return <ReadonlyKvInfo fields={fields} entity={entity} schema={schema} />
    }
    if (section.kind === 'preview') {
      return <PreviewInfo field={section.previewField ?? fields[0]} entity={entity} assetUrl={assetUrl}
        vacantPending={vacantPending} runActive={runActive} />
    }
    if (section.kind === 'asset') {
      const f = section.assetField ?? fields[0]
      const fs = f ? schema?.properties?.[f] : undefined
      const multi = !!f && (section.multiple || isAssetRefArraySchema(fs) || Array.isArray(entity.data[f]))
      return (
        multi ? (
          <AssetRefInput
            multiple
            value={f ? asAssetRefList(entity.data[f]) : []}
            assetUrl={assetUrl}
            uploadAsset={uploadAsset}
            readOnly={entity.read_only}
            accept={section.accept}
            onChange={(next) => {
              if (!f) return
              commitAsset(f, next)
            }}
          />
        ) : (
          <AssetRefInput
            value={f ? asAssetRefDict(entity.data[f]) : null}
            assetUrl={assetUrl}
            uploadAsset={uploadAsset}
            readOnly={entity.read_only}
            accept={section.accept}
            onChange={(next) => {
              if (!f) return
              commitAsset(f, next)
            }}
          />
        )
      )
    }
    // kind === 'fields'
    return <FieldsInfo fields={fields} entity={entity} schema={schema} status={status} dispatch={dispatch} entityRefOptions={entityRefOptions} aiRoutes={slotCtx?.aiRoutes} />
  }, [section, entity, schema, status, runActive, vacantPending, assetUrl, uploadAsset, entityRefOptions, childEntities, dispatch, commitAsset])

  const infoRegion = <div className="ze-editor__unit-info" data-role="info">{info}</div>
  const actions = section.actions ?? []

  return (
    <div className="ze-editor__unit" data-run-phase={phase} aria-busy={runActive ? 'true' : undefined}>
      {section.collapsible ? (
        <Collapsible title={title} defaultOpen={section.defaultOpen ?? true} stateKey={section.id}>{infoRegion}</Collapsible>
      ) : (
        <>
          <div className="ze-bar" data-edge="top"><span className="ze-text" data-role="title">{title}</span></div>
          {infoRegion}
        </>
      )}
      {actions.length > 0 && (
        <div className="ze-editor__unit-control ze-pad ze-gap" data-role="control" data-p="2" data-gap="2">
          {actions.map((a) => renderFormActionButton({
            action: a, entityType: entity.type, entity, selectionIds, schema, running: runActive, dispatch,
            consumeActionResult,
            busy, setBusy, t, requireFieldsLabel: (fields) => t('slot.requiresFields', { fields }),
            onOpenDialog: (act) => setDialogAction(act),
            onRequestConfirm: (text, run) => setConfirmReq({ text, run }),
            requiresRoute: slotCtx?.commandSpecs?.find((s) => s.id === a.command)?.requires_route,
            aiRoutes: slotCtx?.aiRoutes,
            aiConsent: slotCtx?.aiConsent,
          }))}
          {dialogAction && DialogComp && (
            <DialogComp action={dialogAction} entity={entity} dispatch={dispatch} onClose={() => setDialogAction(null)} />
          )}
          {confirmReq && (
            <ConfirmDialog
              message={confirmReq.text}
              onConfirm={() => { confirmReq.run(); setConfirmReq(null) }}
              onCancel={() => setConfirmReq(null)}
            />
          )}
        </div>
      )}
    </div>
  )
}
