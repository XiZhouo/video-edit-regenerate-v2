/**
 * FormZone — 表单字段内容区(functional zone;SPEC §2B `form`)
 *
 * 固定骨架(组件画死):form-body + 字段循环(控件按 schema enum/format/min-max 与值类型派生:
 * select / color / checkbox / slider / number / JSON textarea / text)+ x-group 折叠分组 +
 * 运行态(error→实体内附着错误;仅空 pending→等高骨架;已有内容的 pending/running 保留控件)。
 * 复用 `.ze-field-row*`(L2 组件原子 field-row.css:行高系统/多行例外/run-phase 骨架/响应式)+
 * `.ze-editor__form-body`(L4 结构,内距/行距贴 .ze-pad/.ze-gap)。
 * 受控:entity/schema 进、dispatch 出。草稿(draft / jsonText / invalid)与写回逻辑自包含。
 *
 * 多选聚合编辑(entities.length>1 时,opt-in):同值字段直接批量改,异值字段显「—」混合占位
 * (checkbox 用原生 indeterminate);任何提交走 entity.batch_update(单 Operation,undo 一键
 * 回滚,每实体各自 CAS)。聚合判定在 aggregateFields(纯函数可单测)。
 */

import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useLocale } from '@ze/core/04-user-facing/05-preferences'
import { ErrorPlaceholder } from '../lib/Skeleton'
import { Collapsible } from '../primitives/InspectorSection'
import { FieldRow } from './FieldRow'
import { i18nDataField } from '../lib/fieldSchema'
import { aggregateFieldList, aggregateFields, batchUpdateArgs } from './aggregateFields'
import { useScrollAffordance } from '../lib/useScrollAffordance'
import { isVacantPendingEntity } from '../lib/entityRunPresentation'
import type { AssetRefDict, AssetUrlFn, DispatchFn, EntityDict, EntityRefOptions, EntityRunStatus, EntityTypeSchema, FieldSchema } from '../types'

export type FormZoneProps = {
  /** active 实体(单选编辑对象;多选聚合时也是 status/标题的归属)。 */
  entity: EntityDict
  schema?: EntityTypeSchema
  dispatch?: DispatchFn
  status?: EntityRunStatus
  /** 多选聚合编辑的选区全集(含 active;length>1 才进聚合模式)。 */
  entities?: EntityDict[]
  /** asset 字段(format:'asset')预览取 URL;经 FieldRow 透传(缺则只显占位)。 */
  assetUrl?: AssetUrlFn
  /** asset 字段上传缝;经 FieldRow 透传(缺则 asset 字段降级只读预览)。 */
  uploadAsset?: (bytes: Uint8Array, mime: string, ext?: string) => Promise<AssetRefDict>
  /** entity_ref 字段候选表(按绑定类型分桶;经 FieldRow 透传;缺则该字段降级文本输入)。 */
  entityRefOptions?: EntityRefOptions
}

function fieldsOf(entity: EntityDict, schema?: EntityTypeSchema): [string, FieldSchema][] {
  if (schema?.properties) return Object.entries(schema.properties)
  return Object.keys(entity.data).map((k) => [k, {}])
}

export function FormZone({ entity, schema, dispatch, status, entities, assetUrl, uploadAsset, entityRefOptions }: FormZoneProps): ReactNode {
  const { locale } = useLocale()
  // 多选聚合模式:roster+versions 串作为重算/重置键(任一实体被外部更新即失效)
  const multiKey = entities && entities.length > 1
    ? entities.map((e) => `${e.id}:${e.version}`).join('|') : ''
  const multi = multiKey ? entities! : null
  const agg = useMemo(
    () => (multi ? aggregateFields(multi, schema) : null),
    [multiKey, schema],  // eslint-disable-line react-hooks/exhaustive-deps
  )
  // 草稿基底:单选=entity.data;聚合=同值字段(mixed 字段不入 draft,控件显「—」占位)
  const baseData = useMemo(() => {
    if (!agg) return entity.data
    const out: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(agg)) if (!v.mixed) out[k] = v.value
    return out
  }, [agg, entity.data])

  const [draft, setDraft] = useState<Record<string, unknown>>(baseData)
  const [jsonText, setJsonText] = useState<Record<string, string>>({})
  const [invalid, setInvalid] = useState<Record<string, boolean>>({})
  const bodyRef = useScrollAffordance('y')

  // entity 换了(不同 id 或被外部更新 bump version)/ 选区变了 / 聚合基底变了(含 schema
  // 注入变化经 agg→baseData 传导)→ 重置 draft。baseData 入依赖防 schema 单独变化时草稿陈旧。
  useEffect(() => {
    setDraft(baseData)
    setJsonText({})
    setInvalid({})
  }, [entity.id, entity.version, multiKey, baseData])

  const readOnly = multi ? multi.some((e) => e.read_only) : entity.read_only

  const commit = (field: string, value: unknown) => {
    if (multi) {
      // mixed 字段无共享基准值,提交即覆盖全选区;同值字段沿用"未变不发"守卫
      if (!agg?.[field]?.mixed && value === baseData[field]) return
      dispatch?.('entity.batch_update', batchUpdateArgs(entity.type, multi, field, value))
      return
    }
    if (value === entity.data[field]) return
    dispatch?.('entity.update', {
      type: entity.type, id: entity.id,
      patches: { [field]: value },
      expected_version: entity.version,
    })
  }

  const phase = status?.phase
  const loading = isVacantPendingEntity(entity, status)

  const renderField = ([field, fs]: [string, FieldSchema]): ReactNode => {
    // 按 UI 语言把「取值/提交」绑到对应语言的伴生字段(x-ze-i18n-field);标签仍走概念字段 fs。
    const df = i18nDataField(fs, field, locale)
    const mixed = agg?.[df]?.mixed === true
    const required = fs.required === true || (schema?.required?.includes(field) ?? false)
    return (
      <FieldRow
        key={field}
        field={field} fs={fs}
        baseValue={baseData[df]} value={draft[df]}
        mixed={mixed} required={required} readOnly={readOnly}
        loading={loading} phase={phase}
        jsonText={df in jsonText ? jsonText[df] : undefined}
        jsonInvalid={invalid[df] === true}
        onDraft={(value) => setDraft({ ...draft, [df]: value })}
        onCommit={(value) => commit(df, value)}
        onJsonText={(text) => setJsonText({ ...jsonText, [df]: text })}
        onJsonInvalid={(inv) => setInvalid({ ...invalid, [df]: inv })}
        assetUrl={assetUrl}
        uploadAsset={uploadAsset}
        entityRefOptions={entityRefOptions}
        siblingData={draft}
      />
    )
  }

  // 按 schema 的 x-group 分组:无组字段平铺在前,命名组各进一个 Collapsible(默认展开)。
  const allFields = multi ? aggregateFieldList(multi, schema) : fieldsOf(entity, schema)
  const ungrouped = allFields.filter(([, fs]) => !(fs as { 'x-group'?: unknown })['x-group'])
  const groupOrder: string[] = []
  const grouped: Record<string, [string, FieldSchema][]> = {}
  for (const [field, fs] of allFields) {
    const g = (fs as { 'x-group'?: unknown })['x-group']
    if (typeof g !== 'string' || !g) continue
    if (!grouped[g]) { grouped[g] = []; groupOrder.push(g) }
    grouped[g].push([field, fs])
  }

  return (
    <div ref={bodyRef} className="ze-editor__form-body ze-scroll-fade-y ze-pad ze-gap" data-p="2" data-gap="1" data-run-phase={phase}>
      {phase === 'error' && (
        <div className="ze-editor__form-status">
          <ErrorPlaceholder compact
            error={status?.error}
            onRetry={status?.run_id ? () => dispatch?.('pipeline.retry_failed', {
              pipeline_id: status.run_id, item_ids: [entity.id],
            }) : undefined}
          />
        </div>
      )}
      {ungrouped.map(renderField)}
      {groupOrder.map((g) => (
        <Collapsible key={g} title={g}>
          {grouped[g].map(renderField)}
        </Collapsible>
      ))}
    </div>
  )
}
