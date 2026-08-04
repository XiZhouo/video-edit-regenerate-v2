/**
 * fieldSchema — schema→控件的派生助手,FieldRow / ParamPanel 共用(去重两处 min/max/step 逻辑)。
 *
 * 硬范围 `minimum`/`maximum` 是**校验约束**(field_types.py 据此判非法)。
 * 软范围 `x-softMinimum`/`x-softMaximum` 是**纯 UI 范围**:让"有界但容忍越界(导入/历史数据)"的连续参数
 * (scale/rotate/zoom)渲染成滑条,而不把越界值判为非法。两者都在 → 硬优先;只有软 → 仍出滑条。
 *
 * 这是 slider 回归的根治点:此前 FieldRow/ParamPanel 只读硬范围,下游用 x-soft 声明的连续参数渲不出滑条;
 * 修复曾被放进下游受管层拷贝、随 Skill.Sync 被覆盖删除(见 CHANGE-PROPAGATION.md)。改在 core 源即可存活。
 */

import type { FieldSchema } from '../types'

export type NumericRangeMeta = {
  min?: number
  max?: number
  step?: number
}

/** 只接受有限数字(挡掉 NaN / Infinity / 非数字),否则 undefined。 */
export function finiteNumber(v: unknown): number | undefined {
  return typeof v === 'number' && Number.isFinite(v) ? v : undefined
}

/** 取字段的滑条范围:硬 minimum/maximum 优先,回退软 x-softMinimum/x-softMaximum;step 取 multipleOf > step > 整型默认 1。 */
export function numericRangeMeta(fs: FieldSchema, kind?: string): NumericRangeMeta {
  return {
    min: finiteNumber(fs.minimum) ?? finiteNumber(fs['x-softMinimum']),
    max: finiteNumber(fs.maximum) ?? finiteNumber(fs['x-softMaximum']),
    step: finiteNumber(fs.multipleOf) ?? finiteNumber(fs.step) ?? (kind === 'integer' ? 1 : undefined),
  }
}

/**
 * 按 UI 语言解析字段的「数据绑定键」:schema 声明 `x-ze-i18n-field: {zh:'materialZh'}` 时,
 * 当前 locale 命中就用对应语言的伴生字段取值/提交(如中文界面用 materialZh),标签仍走概念字段的
 * schema(如「材质」)。缺声明 / 该语言无映射 / 映射非字符串 → 原字段(零行为变化)。
 * 用途:属性面板「跟界面语言、同一格子只显一种语言」——数据层存双语(base 英文 + *Zh 中文),
 * 显示层按 locale 切。仅换数据键、不动 core 其它逻辑,是加性能力。
 */
export function i18nDataField(fs: FieldSchema, field: string, locale: string): string {
  const map = (fs as { 'x-ze-i18n-field'?: Record<string, string> })['x-ze-i18n-field']
  const mapped = map && typeof map[locale] === 'string' ? map[locale].trim() : ''
  return mapped || field
}

/**
 * 按 UI 语言解析字段的【显示标签】:schema 声明 `x-ze-label-i18n: {en:'Material', zh:'材质'}` 时,
 * 当前 locale 命中就用对应语言标签;否则回退 schema `label`(单语 mod 填的数据显示名),再回退字段键。
 * 对应 05-i18n README『chrome vs 数据边界』:数据显示名默认不随 locale 切;真双语 skill 经此声明「自给
 * locale 感知的 label」(框架按 locale 选值),仍属数据层(非 t() chrome key,故 i18n-english 门禁不扫)。
 */
export function i18nLabel(fs: FieldSchema, field: string, locale: string): string {
  const map = (fs as { 'x-ze-label-i18n'?: Record<string, string> })['x-ze-label-i18n']
  const mapped = map && typeof map[locale] === 'string' ? map[locale].trim() : ''
  if (mapped) return mapped
  const label = (fs as { label?: unknown }).label
  return typeof label === 'string' && label ? label : field
}

/**
 * 按 UI 语言解析 enum 选项的【显示名】:schema 声明 `x-ze-enum-labels: {value: '人话名' | {en,zh}}`
 * 时,当前 locale 命中就用对应语言名(双语),或直接用单语字符串;命中不了任一语言时兜底任一非空值。
 * 缺声明/未映射 → 回退旧扁平 `enumLabels[value]`(单语兼容通路),再回退枚举原值(裸 token)。
 * 与 i18nLabel 同规:数据显示名(非 t() chrome key);全程回退安全,不声明则原样显示原值。
 */
export function enumOptionLabel(fs: FieldSchema, value: unknown, locale: string): string {
  const key = String(value)
  const map = (fs as { 'x-ze-enum-labels'?: Record<string, unknown> })['x-ze-enum-labels']
  const entry = map ? map[key] : undefined
  if (typeof entry === 'string' && entry) return entry
  if (entry && typeof entry === 'object') {
    const byLocale = entry as Record<string, unknown>
    const hit = byLocale[locale]
    if (typeof hit === 'string' && hit) return hit
    for (const v of Object.values(byLocale)) {
      if (typeof v === 'string' && v) return v   // 任一语言兜底(声明了名字就不回落裸值)
    }
  }
  const legacy = (fs as { enumLabels?: Record<string, string> }).enumLabels
  const legacyHit = legacy?.[key]
  if (typeof legacyHit === 'string' && legacyHit) return legacyHit
  return key
}
