/**
 * ParamPanel — schema 驱动的参数 / 属性控件栈(框架级 primitive)
 *
 * 是 FormEditor 字段循环的泛化:读一个 EntityTypeSchema.properties,按字段 type 派生控件
 * (enum→select / boolean→switch / number+min&max→slider / number→数字 / string→文本)。
 * canvas2d / scene3d / task 的 inspector 复用它;form editor 也可内部复用(去重)。
 *
 * 实用极简:advancedFields 里的字段进 Collapsible 默认折叠 —— 默认路径只露常改的几个。
 * 受控:values 进,onCommit(field,value) 出(由上层 dispatch entity.update)。
 * status='loading' 时渲染骨架(消灭 PropertyPanel vs Ghost 的重复:同一组件一个 prop 分支)。
 */

import { useEffect, useState, type ReactNode } from 'react'
import { useT, useLocale } from '@ze/core/04-user-facing/05-preferences'
import { UserPicker } from '@ze/core/04-user-facing/03-authentication'
import { Collapsible } from './InspectorSection'
import { numericRangeMeta, enumOptionLabel } from '../lib/fieldSchema'
import { SkeletonText } from '../lib/Skeleton'
import { useScrollAffordance } from '../lib/useScrollAffordance'
import type { EntityTypeSchema, FieldSchema, LoadingStatus } from '../types'

export type ParamPanelProps = {
  schema: EntityTypeSchema
  values: Record<string, unknown>
  onCommit: (field: string, value: unknown) => void
  readOnly?: boolean
  status?: LoadingStatus
  /** 这些字段进"高级"折叠区(默认折叠);其余字段常驻 */
  advancedFields?: string[]
  /** 必填字段(label 加 *;空值 Run/提交置灰由调用方判,本组件只标注)。 */
  requiredFields?: string[]
}

function fieldKind(fs: FieldSchema, value: unknown): string {
  if (fs.type) return String(fs.type)
  if (typeof value === 'boolean') return 'boolean'
  if (typeof value === 'number') return 'number'
  return 'string'
}

function FieldControl({
  field, fs, value, readOnly, required, onCommit,
}: {
  field: string; fs: FieldSchema; value: unknown; readOnly?: boolean; required?: boolean
  onCommit: (field: string, value: unknown) => void
}): ReactNode {
  const [draft, setDraft] = useState<unknown>(value)
  useEffect(() => { setDraft(value) }, [value])
  const { locale } = useLocale()

  const enumVals = Array.isArray(fs.enum) ? (fs.enum as unknown[]) : null
  const kind = fieldKind(fs, value)
  // 硬 minimum/maximum 优先,回退软 x-softMinimum/x-softMaximum;step 取 multipleOf>step>整型默认
  const { min, max, step } = numericRangeMeta(fs, kind)
  const userWidget = (fs as { 'x-ze-widget'?: string })['x-ze-widget']
  const isUserPicker = userWidget === 'user-picker' || (fs as { format?: string }).format === 'ze-user'

  let control: ReactNode
  if (isUserPicker) {
    control = (
      <UserPicker
        value={typeof value === 'string' ? value : ''}
        disabled={readOnly}
        size="sm"
        channel={String((fs as { 'x-ze-channel'?: unknown })['x-ze-channel'] || '')}
        allowRaw={Boolean((fs as { 'x-ze-raw-fallback'?: unknown })['x-ze-raw-fallback'])}
        onChange={(next) => onCommit(field, next)}
      />
    )
  } else if (enumVals) {
    control = (
      <span className="ze-select" data-size="sm">
        <select
          className="ze-select__control"
          value={String(value ?? '')}
          disabled={readOnly}
          onChange={(e) => onCommit(field, e.target.value)}
        >
          {enumVals.map((v) => <option key={String(v)} value={String(v)}>{enumOptionLabel(fs, v, locale)}</option>)}
        </select>
      </span>
    )
  } else if (kind === 'boolean') {
    control = (
      <input
        type="checkbox" role="switch" className="ze-switch" data-size="sm"
        checked={Boolean(value)} disabled={readOnly}
        onChange={(e) => onCommit(field, e.target.checked)}
      />
    )
  } else if ((kind === 'number' || kind === 'integer') && min !== undefined && max !== undefined) {
    control = (
      <span className="ze-row" data-gap="2">
        <input
          type="range" className="ze-slider"
          min={min} max={max} step={step ?? (kind === 'integer' ? 1 : 'any')}
          value={Number(draft ?? min)} disabled={readOnly}
          onChange={(e) => setDraft(Number(e.target.value))}
          onMouseUp={(e) => onCommit(field, Number((e.target as HTMLInputElement).value))}
          onKeyUp={(e) => onCommit(field, Number((e.target as HTMLInputElement).value))}
        />
        <span className="ze-text ze-field-row__value" data-role="tertiary">{String(draft ?? '')}</span>
      </span>
    )
  } else if (kind === 'number' || kind === 'integer') {
    control = (
      <input
        type="number" className="ze-input" data-size="sm"
        value={draft === null || draft === undefined ? '' : String(draft)} disabled={readOnly}
        onChange={(e) => setDraft(e.target.value === '' ? null : Number(e.target.value))}
        onBlur={(e) => onCommit(field, e.target.value === '' ? null : Number(e.target.value))}
      />
    )
  } else {
    control = (
      <input
        type="text" className="ze-input" data-size="sm"
        value={draft === null || draft === undefined ? '' : String(draft)} disabled={readOnly}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={(e) => onCommit(field, e.target.value)}
      />
    )
  }

  return (
    <label className="ze-field-row" data-density="compact">
      <span className="ze-field-row__label">{(fs.label as string) || field}{required ? ' *' : ''}</span>
      {control}
    </label>
  )
}

export function ParamPanel({
  schema, values, onCommit, readOnly, status, advancedFields = [], requiredFields = [],
}: ParamPanelProps): ReactNode {
  const t = useT()
  const props = schema.properties ?? {}
  const advanced = new Set(advancedFields)
  const requiredSet = new Set(requiredFields)
  const entries = Object.entries(props)
  const basic = entries.filter(([f]) => !advanced.has(f))
  const adv = entries.filter(([f]) => advanced.has(f))
  const panelRef = useScrollAffordance('y')

  if (status === 'loading') {
    return (
      <div ref={panelRef} className="ze-param ze-scroll-fade-y ze-stack" data-gap="1" data-pad="2">
        {entries.map(([f]) => (
          <label key={f} className="ze-field-row" data-density="compact"><span className="ze-field-row__label">{f}</span><SkeletonText /></label>
        ))}
      </div>
    )
  }

  return (
    <div ref={panelRef} className="ze-param ze-scroll-fade-y ze-stack" data-gap="1" data-pad="2">
      {basic.map(([f, fs]) => (
        <FieldControl key={f} field={f} fs={fs} value={values[f]} readOnly={readOnly} required={requiredSet.has(f)} onCommit={onCommit} />
      ))}
      {adv.length > 0 && (
        <Collapsible title={t('editor.advanced')} defaultOpen={false}>
          {adv.map(([f, fs]) => (
            <FieldControl key={f} field={f} fs={fs} value={values[f]} readOnly={readOnly} required={requiredSet.has(f)} onCommit={onCommit} />
          ))}
        </Collapsible>
      )}
    </div>
  )
}
