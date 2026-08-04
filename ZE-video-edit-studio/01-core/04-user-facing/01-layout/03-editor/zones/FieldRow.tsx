/**
 * FieldRow — 单字段行(受控纯组件;form 字段渲染的唯一真源)
 *
 * 从 FormZone.renderField 抽出的 8 控件分支(enum→select / color / boolean→checkbox /
 * bounded→slider / number / json→textarea / text;loading→骨架)。**纯受控**:draft / jsonText /
 * invalid 三态由调用方(FormZone 或 InfoControlUnit)持有并经 props 注入,FieldRow 只渲染 +
 * 经回调上抛 setDraft/setJsonText/setInvalid/commit;自身零 state(行为对齐原内联实现,字节等价)。
 *
 * 复用:`.ze-field-row*`(L2 field-row.css)+ .ze-select/.ze-input/.ze-textarea/.ze-checkbox 原子 +
 * Slider primitive。jsonText/invalid 是 JSON textarea 的「编辑中文本 / 解析失败」两态(原 FormZone
 * 语义不变)。
 */

import type { ReactNode } from 'react'
import { useT, useLocale } from '@ze/core/04-user-facing/05-preferences'
import { UserPicker } from '@ze/core/04-user-facing/03-authentication'
import { CapabilityModelPicker } from '../../CapabilityModelPicker'
import { SkeletonText, SkeletonBlock } from '../lib/Skeleton'
import { numericRangeMeta, i18nLabel, enumOptionLabel } from '../lib/fieldSchema'
import { fieldVisibleForData } from '../lib/fieldVisibility'
import { Slider } from '../primitives/Slider'
import { AssetRefInput } from '../primitives/AssetRefInput'
import type { AssetRefDict, AssetUrlFn, EntityRefOptions, EntityRunStatus, FieldSchema } from '../types'
import type { AiRoutesState } from '../../../03-authentication/aiRoutes'

/** asset_ref dict(含 sha256)守卫;否则 null。 */
function asAssetRefDict(v: unknown): AssetRefDict | null {
  return v && typeof v === 'object' && 'sha256' in (v as Record<string, unknown>) ? (v as AssetRefDict) : null
}

function asAssetRefList(v: unknown): AssetRefDict[] {
  if (Array.isArray(v)) return v.map(asAssetRefDict).filter((r): r is AssetRefDict => !!r)
  const one = asAssetRefDict(v)
  return one ? [one] : []
}

/** array<asset_ref> 字段(schema 声明多资产):type:'array' 且 format:'asset' 或 items 为 asset_ref。 */
function isAssetRefArraySchema(fs: FieldSchema): boolean {
  if (fs.type !== 'array') return false
  const items = fs.items as FieldSchema | undefined
  return fs.format === 'asset' || items?.type === 'asset_ref' || items?.format === 'asset'
}

/** enum 选项按 AI 路线可用性门控(x-ze-enum-option-routes:{optionValue: route | route[]})。
 *  一个模型可由多条路线产出(如 GPT Image 2 = 内网服务 或 codex 或 openai-api);声明其路线集合,
 *  **任一路线可用 → 该选项可选;全不可用 → disabled + 「需先连接」标记**。语义:「显示所有可能性、
 *  只区别显示能用的、只能选择能用的」。未声明该扩展、aiRoutes 未加载、或该选项无 route 声明 →
 *  false(不门控,加性零影响,与 AiRouteGate 未加载即透传同律)。 */
function enumOptionRouteBlocked(fs: FieldSchema, optionValue: string, aiRoutes?: AiRoutesState): boolean {
  const map = (fs as { 'x-ze-enum-option-routes'?: unknown })['x-ze-enum-option-routes']
  if (!map || typeof map !== 'object' || !aiRoutes?.loaded) return false
  const spec = (map as Record<string, unknown>)[optionValue]
  const routes = typeof spec === 'string' ? [spec]
    : Array.isArray(spec) ? spec.filter((r): r is string => typeof r === 'string')
    : []
  if (!routes.length) return false
  // 可用 = 任一声明路线在 aiRoutes 里存在且可用;全不可用 → 置灰。
  return !routes.some((route) => {
    const info = aiRoutes.routes.find((r) => r.route === route)
    return !!info && info.available
  })
}

/** enum 选项随兄弟字段联动(x-ze-enum-by-field:{field,aliases?,values}):按 siblings[field] 过滤选项。
 *  spec 缺失/不匹配 → 原样返回(零行为变化)。 */
function dependentEnumOptions(fs: FieldSchema, options: unknown[] | null, siblings?: Record<string, unknown>): unknown[] | null {
  if (!options) return null
  const spec = (fs as { 'x-ze-enum-by-field'?: unknown })['x-ze-enum-by-field']
  if (!spec || typeof spec !== 'object' || !siblings) return options
  const data = spec as {
    field?: unknown
    aliases?: Record<string, string>
    values?: Record<string, unknown[]>
  }
  if (typeof data.field !== 'string' || !data.values) return options
  const raw = String(siblings[data.field] ?? '')
  const key = data.aliases?.[raw] ?? raw
  const allowed = data.values[key]
  if (!Array.isArray(allowed)) return options
  const allowedSet = new Set(allowed.map((v) => String(v)))
  return options.filter((option) => allowedSet.has(String(option)))
}

export interface FieldRowProps {
  field: string
  fs: FieldSchema
  /** 基底值(单选=entity.data[field];聚合=同值字段值);派生控件类型/取色器回填。 */
  baseValue: unknown
  /** 当前草稿值(受控)。 */
  value: unknown
  /** 聚合模式异值占位(混合);控件显「—」+ indeterminate。 */
  mixed?: boolean
  /** 必填(label 加 *;空值 aria-invalid)。 */
  required?: boolean
  readOnly?: boolean
  /** 仅空 pending 占位渲骨架；已有内容的 pending/running 继续渲真实控件。 */
  loading?: boolean
  /** 运行相位只装饰当前字段行，不决定是否替换内容。 */
  phase?: EntityRunStatus['phase']
  /** JSON textarea「编辑中文本」(本字段未在编辑则 undefined,回落 JSON.stringify)。 */
  jsonText?: string
  /** JSON 解析失败(本字段);标 aria-invalid。 */
  jsonInvalid?: boolean
  /** 草稿写入(本地暂存,不提交)。 */
  onDraft: (value: unknown) => void
  /** 提交(写回 entity;FormZone 内部判「未变不发」)。可返 Promise 供上传/粘贴队列 await。 */
  onCommit: (value: unknown) => void | Promise<unknown>
  /** JSON textarea 编辑中文本写入。 */
  onJsonText: (text: string) => void
  /** JSON 解析有效性标记。 */
  onJsonInvalid: (invalid: boolean) => void
  /** asset 字段(format:'asset' / type:'asset_ref')预览取 URL;缺则只显占位。 */
  assetUrl?: AssetUrlFn
  /** asset 字段上传缝;缺省 → asset 字段降级只读预览(AssetRefInput 内部判)。 */
  uploadAsset?: (bytes: Uint8Array, mime: string, ext?: string) => Promise<AssetRefDict>
  /** entity_ref 字段的候选表(按绑定类型 schema `to` 分桶;装配层从 entitiesByType 预派生)。
   *  有对应桶 → 渲候选下拉;缺(无 `to` / 该类型无候选 / 未透传)→ 降级为写 {"$ref"} 的文本输入。 */
  entityRefOptions?: EntityRefOptions
  /** 同一实体当前草稿数据;供 schema 扩展做字段间联动(x-ze-visible-when 显隐 / x-ze-enum-by-field 选项)。 */
  siblingData?: Record<string, unknown>
  /** AI 路线态:enum 字段声明 x-ze-enum-option-routes 时,路线不可用的选项 disabled+标记(加性,可选)。 */
  aiRoutes?: AiRoutesState
}

export function FieldRow({
  field, fs, baseValue, value, mixed = false, required = false, readOnly, loading,
  phase, jsonText, jsonInvalid, onDraft, onCommit, onJsonText, onJsonInvalid,
  assetUrl, uploadAsset, entityRefOptions, siblingData, aiRoutes,
}: FieldRowProps): ReactNode {
  const t = useT()
  const { locale } = useLocale()
  const dv = baseValue
  const ftype = fs.type ?? (
    typeof dv === 'boolean' ? 'boolean'
    : Array.isArray(dv) ? 'array'
    : dv && typeof dv === 'object' ? 'object'
    : 'string'
  )
  const rawEnumOpts = Array.isArray((fs as { enum?: unknown[] }).enum) ? (fs as { enum: unknown[] }).enum : null
  const val = value
  const enumOpts = dependentEnumOptions(fs, rawEnumOpts, siblingData)
  const emptyRequired = required && !mixed && (val === '' || val === null || val === undefined)
  const isJson = ftype === 'array' || ftype === 'object'
  const isNumeric = ftype === 'number' || ftype === 'integer'
  const isColor = (fs as { format?: string }).format === 'color'
  const isAsset = (fs as { format?: string }).format === 'asset' || ftype === 'asset_ref'
  const isAssetList = isAssetRefArraySchema(fs)
  // entity_ref:值形态 {"$ref":"type/id"} dict(非裸字符串)。绑定类型取 schema `to`;有候选桶
  // 渲下拉、否则降级文本输入,两路都提交 dict —— 根治「填 JSON 字符串被原样以 string 落库」。
  const isEntityRef = ftype === 'entity_ref'
  const refTo = (fs as { to?: string }).to
  const refOpts = isEntityRef && refTo ? entityRefOptions?.[refTo] : undefined
  // 当前引用的 "type/id":dict 取 $ref;历史脏值(裸字符串)原样显示以便纠正;否则空。
  const refOf = (v: unknown): string => {
    if (v && typeof v === 'object' && typeof (v as { $ref?: unknown }).$ref === 'string') return (v as { $ref: string }).$ref
    if (typeof v === 'string') return v
    return ''
  }
  const userWidget = (fs as { 'x-ze-widget'?: string })['x-ze-widget']
  const isUserPicker = userWidget === 'user-picker' || (fs as { format?: string }).format === 'ze-user'
  // 受管模型选择器(规划 §3.6):字段标 x-ze-widget:'capability-model' → 渲 Build 唯一受管
  // CapabilityModelPicker(消费 /api/ai/options,全候选 + 可用性 + 9 类错误码 + 管理入口),
  // 取代自建 enum 下拉。query 来自字段 schema 扩展:x-ze-feature-id / x-ze-capability(缺省 image)/
  // x-ze-require(细能力数组)/ x-ze-intent(质量意图)。字段值 = 选中 model_id。
  const isModelPicker = userWidget === 'capability-model'
  const modelPickerQuery = isModelPicker ? {
    featureId: String((fs as { 'x-ze-feature-id'?: unknown })['x-ze-feature-id'] || 'generic'),
    capability: String((fs as { 'x-ze-capability'?: unknown })['x-ze-capability'] || 'image'),
    require: Array.isArray((fs as { 'x-ze-require'?: unknown })['x-ze-require'])
      ? ((fs as { 'x-ze-require'?: unknown[] })['x-ze-require'] as unknown[]).map(String)
      : undefined,
    intent: (fs as { 'x-ze-intent'?: unknown })['x-ze-intent']
      ? String((fs as { 'x-ze-intent'?: unknown })['x-ze-intent'])
      : undefined,
  } : null
  // per-item 覆盖场景:字段标 x-ze-allow-default,则在 picker 前置「用项目默认」空值选项
  // (label 取 x-ze-default-label,回落 enumOptionLabel 的空值标签或通用「默认」)。
  const modelPickerAllowDefault = isModelPicker && (fs as { 'x-ze-allow-default'?: unknown })['x-ze-allow-default']
    ? { label: String((fs as { 'x-ze-default-label'?: unknown })['x-ze-default-label'] || enumOptionLabel(fs, '', locale) || t('editor.entityRefEmpty')) }
    : undefined
  // 多行文本(提示词等):schema 标 x-ze-widget:'textarea' 或 format:'multiline' → 渲多行 textarea 而非单行 input。
  const isMultiline = userWidget === 'textarea' || (fs as { format?: string }).format === 'multiline'
  // 硬 minimum/maximum 优先,回退软 x-softMinimum/x-softMaximum(有界连续参数渲滑条但不硬校验)
  const { min, max, step } = numericRangeMeta(fs, ftype)
  // mixed 数值无共享基准,滑杆退化为数字输入(空值 + 「—」占位)
  const useSlider = isNumeric && min !== undefined && max !== undefined && !mixed
  const control = isColor ? 'color' : isUserPicker ? 'user-picker' : useSlider ? 'slider' : undefined

  // 声明式显隐(x-ze-visible-when):按兄弟字段值决定本行是否渲染(如选了某模型才显对应参数)。
  if (!fieldVisibleForData(fs, siblingData)) return null

  return (
    <label
      className="ze-field-row" data-control={control} data-density="compact"
      /* W1-6:字段身份落到 DOM。变更卡的字段 chip 靠它把用户送到被改的那一行
         (App.tsx 收到带 field 的 reveal_entity 时 querySelector 查的就是这个属性)。 */
      data-ze-field={field}
      data-multiline={(isJson || isAsset || isMultiline) ? 'true' : undefined} data-run-phase={phase}
      data-mixed={mixed ? 'true' : undefined}
      title={mixed ? t('editor.mixedValues') : undefined}
    >
      <span className="ze-field-row__label">{i18nLabel(fs, field, locale)}{required ? ' *' : ''}</span>
      {loading ? (
        (isJson || isAsset || isMultiline) ? <SkeletonBlock /> : <SkeletonText />
      ) : isModelPicker ? (
        <CapabilityModelPicker
          query={modelPickerQuery!}
          value={typeof val === 'string' ? val : ''}
          disabled={readOnly}
          allowDefault={modelPickerAllowDefault}
          onChange={(next) => { onDraft(next); onCommit(next) }}
        />
      ) : isAsset ? (
        isAssetList ? (
          <AssetRefInput
            multiple
            value={asAssetRefList(val)}
            assetUrl={assetUrl}
            uploadAsset={uploadAsset}
            readOnly={readOnly}
            accept={(fs as { accept?: string }).accept}
            onChange={(refs) => { onDraft(refs); return onCommit(refs) }}
          />
        ) : (
          <AssetRefInput
            value={asAssetRefDict(val)}
            assetUrl={assetUrl}
            uploadAsset={uploadAsset}
            readOnly={readOnly}
            accept={(fs as { accept?: string }).accept}
            onChange={(ref) => { onDraft(ref); return onCommit(ref) }}
          />
        )
      ) : isEntityRef ? (
        refOpts && refOpts.length > 0 ? (
          <span className="ze-select" data-size="sm" aria-invalid={emptyRequired ? 'true' : undefined}>
            <select
              className="ze-select__control"
              value={refOf(val)}
              disabled={readOnly}
              onChange={(e) => { const rv = e.target.value; const next = rv ? { $ref: rv } : null; onDraft(next); onCommit(next) }}
            >
              <option value="">{t('editor.entityRefEmpty')}</option>
              {refOpts.map((o) => <option key={o.id} value={`${refTo}/${o.id}`}>{o.label}</option>)}
            </select>
          </span>
        ) : (
          <input
            type="text"
            className="ze-input"
            data-size="sm"
            aria-invalid={emptyRequired ? 'true' : undefined}
            value={refOf(val)}
            placeholder={refTo ? `${refTo}/…` : undefined}
            disabled={readOnly}
            onChange={(e) => onDraft(e.target.value ? { $ref: e.target.value } : null)}
            onBlur={(e) => { const s = e.target.value.trim(); onCommit(s ? { $ref: s } : null) }}
          />
        )
      ) : enumOpts ? (
        <span className="ze-select" data-size="sm" aria-invalid={emptyRequired ? 'true' : undefined}>
          <select
            className="ze-select__control"
            value={val === null || val === undefined ? '' : String(val)}
            disabled={readOnly}
            onChange={(e) => { onDraft(e.target.value); onCommit(e.target.value) }}
          >
            {/* 非必填要有「不选」的出口,而这个出口**必须有名字**。同文件上方 entity_ref
                的空选项早就是 t('editor.entityRefEmpty'),枚举这条一直漏着 —— 于是选单
                顶上是一行纯空白,读不出它是「清空」还是渲染坏了。
                另一半:enum 里若已含空值(mod 自己声明了 `""` 并给了更贴切的标签,如 foundry
                任务级 gptSize 的「(用项目默认)」),就不能再补 —— 那会变成同一个值两个入口。
                mod 的具体说法优先于框架的通用说法。 */}
            {(!required || mixed) && !enumOpts.some((o) => String(o) === '') && (
              <option value="">{t('editor.enumEmpty')}</option>
            )}
            {enumOpts.map((o) => {
              const ov = String(o)
              const blocked = enumOptionRouteBlocked(fs, ov, aiRoutes)
              return (
                <option key={ov} value={ov} disabled={blocked}>
                  {enumOptionLabel(fs, o, locale)}{blocked ? ` ${t('editor.optionNeedsConnect')}` : ''}
                </option>
              )
            })}
          </select>
        </span>
      ) : isColor ? (
        <input
          type="color"
          className="ze-field-row__color"
          value={typeof val === 'string' && /^#[0-9a-fA-F]{6}$/.test(val) ? val : ''}
          disabled={readOnly}
          onChange={(e) => onDraft(e.target.value)}
          onBlur={(e) => onCommit(e.target.value)}
        />
      ) : isUserPicker ? (
        <UserPicker
          value={typeof val === 'string' ? val : ''}
          disabled={readOnly}
          size="sm"
          channel={String((fs as { 'x-ze-channel'?: unknown })['x-ze-channel'] || '')}
          allowRaw={Boolean((fs as { 'x-ze-raw-fallback'?: unknown })['x-ze-raw-fallback'])}
          onChange={(next) => { onDraft(next); onCommit(next) }}
        />
      ) : ftype === 'boolean' ? (
        <input
          type="checkbox"
          className="ze-checkbox"
          checked={Boolean(val)}
          ref={(el) => { if (el) el.indeterminate = mixed && val === undefined }}
          disabled={readOnly}
          onChange={(e) => { onDraft(e.target.checked); onCommit(e.target.checked) }}
        />
      ) : useSlider ? (
        <Slider
          value={typeof val === 'number' ? val : Number(val)}
          min={min!} max={max!} step={step}
          disabled={readOnly}
          onChange={(v) => onDraft(v)}
          onCommit={(v) => onCommit(v)}
        />
      ) : isNumeric ? (
        <input
          type="number"
          className="ze-input"
          data-size="sm"
          aria-invalid={emptyRequired ? 'true' : undefined}
          value={val === null || val === undefined ? '' : String(val)}
          placeholder={mixed && val === undefined ? '—' : undefined}
          disabled={readOnly}
          onChange={(e) => onDraft(e.target.value === '' ? null : Number(e.target.value))}
          onBlur={(e) => onCommit(e.target.value === '' ? null : Number(e.target.value))}
        />
      ) : isJson ? (
        <textarea
          className="ze-textarea"
          data-size="sm"
          rows={4}
          aria-invalid={jsonInvalid ? 'true' : undefined}
          value={jsonText !== undefined ? jsonText
            : mixed && val === undefined ? ''
            : JSON.stringify(val ?? (ftype === 'array' ? [] : {}), null, 2)}
          placeholder={mixed && val === undefined ? '—' : undefined}
          disabled={readOnly}
          onChange={(e) => onJsonText(e.target.value)}
          onBlur={(e) => {
            try {
              const parsed = JSON.parse(e.target.value)
              onJsonInvalid(false)
              onDraft(parsed)
              onCommit(parsed)
            } catch {
              onJsonInvalid(true)
            }
          }}
        />
      ) : isMultiline ? (
        <textarea
          className="ze-textarea"
          data-size="sm"
          rows={typeof (fs as { rows?: number }).rows === 'number' ? (fs as { rows: number }).rows : 4}
          aria-invalid={emptyRequired ? 'true' : undefined}
          value={val === null || val === undefined ? '' : String(val)}
          placeholder={mixed && val === undefined ? '—' : undefined}
          disabled={readOnly}
          onChange={(e) => onDraft(e.target.value)}
          onBlur={(e) => onCommit(e.target.value)}
        />
      ) : (
        <input
          type="text"
          className="ze-input"
          data-size="sm"
          aria-invalid={emptyRequired ? 'true' : undefined}
          value={val === null || val === undefined ? '' : String(val)}
          placeholder={mixed && val === undefined ? '—' : undefined}
          disabled={readOnly}
          onChange={(e) => onDraft(e.target.value)}
          onBlur={(e) => onCommit(e.target.value)}
        />
      )}
    </label>
  )
}
