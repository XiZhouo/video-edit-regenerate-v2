/**
 * cardListModel — 卡片列表编辑器(cardlist, legacy gallery)的纯逻辑:过滤 / 卡片尺寸 preset / view_state 解析。
 *
 * 抽成纯模块便于单测(vitest node 环境,不拉 React)。
 * 过滤是视图态(不进 Operation/store):搜索 query + 状态 pill 共同收窄可见集,
 * 选择语义(⌘A/range/marquee 的 orderedIds)随可见集走 —— 过滤后全选=选可见的。
 */

import type { EntityDict } from '../types'
import type { CardSort, GalleryAction, GridFilter } from '../zones/types'

// ── 缩略图尺寸(连续滑杆;旧 data-thumb-size 仅保留给直用 ThumbnailGrid 的兼容入口) ─────
export const THUMB_SIZES = ['xs', 'sm', 'md', 'lg', 'xl'] as const
export type ThumbSize = (typeof THUMB_SIZES)[number]
export const THUMB_SCALE_MIN = 0
export const THUMB_SCALE_MAX = 100

export function asThumbSize(v: unknown): ThumbSize | undefined {
  return THUMB_SIZES.includes(v as ThumbSize) ? (v as ThumbSize) : undefined
}

export function thumbSizeIndex(size: ThumbSize): number {
  return THUMB_SIZES.indexOf(size)
}

export function thumbSizeAtIndex(index: number, fallback: ThumbSize = 'md'): ThumbSize {
  const n = Math.round(index)
  return THUMB_SIZES[Math.min(Math.max(n, 0), THUMB_SIZES.length - 1)] ?? fallback
}

export function clampThumbScale(v: unknown, fallback = 32): number {
  const n = typeof v === 'number' ? v : Number(v)
  if (!Number.isFinite(n)) return clampThumbScale(fallback, 32)
  return Math.min(THUMB_SCALE_MAX, Math.max(THUMB_SCALE_MIN, n))
}

export const THUMB_SIZE_TO_SCALE: Record<ThumbSize, number> = {
  xs: 0, sm: 14, md: 32, lg: 60, xl: 100,
}

export function thumbScaleForSize(size: ThumbSize): number {
  return THUMB_SIZE_TO_SCALE[size]
}

/** 连续滑杆值 → grid min column width(px). 100 档故意给超大值,经 CSS min(100%,x) 自然单列且不溢出。 */
export function thumbGridMinWidthForScale(scale: number): number {
  const s = clampThumbScale(scale)
  if (s >= 99.5) return 9999
  if (s <= 60) {
    const t = s / 60
    return Math.round(72 + (220 - 72) * Math.pow(t, 1.15))
  }
  const t = (s - 60) / 40
  return Math.round(220 + (960 - 220) * Math.pow(t, 1.35))
}

export function thumbListMediaWidthForScale(scale: number): number {
  const w = thumbGridMinWidthForScale(Math.min(clampThumbScale(scale), 92))
  return Math.round(Math.min(320, Math.max(32, w * 0.42)))
}

export function thumbCompactMediaWidthForScale(scale: number): number {
  const w = thumbListMediaWidthForScale(scale)
  return Math.round(Math.min(96, Math.max(24, w * 0.35)))
}

// ── 卡片尺寸(mod 预设·语义档;运行时连续滑杆可覆盖为用户视图偏好) ──────────────────
// 用户口径:每个工具预设最合适的尺寸(语义意图),core 据此设初始 scale 再由 CSS 自适应算列数;
// 运行时不再显示难懂的 LG/SM/MD 循环钮,而是明确的"缩略图尺寸"无级滑杆。
// 语义档内部映射到连续 scale 初值:compact 密 / balanced 中 / spacious 疏。
export const CARD_SIZES = ['compact', 'balanced', 'spacious'] as const
export type CardSize = (typeof CARD_SIZES)[number]

export function asCardSize(v: unknown): CardSize | undefined {
  return CARD_SIZES.includes(v as CardSize) ? (v as CardSize) : undefined
}

/** 语义档 → 旧 data-thumb-size 兼容映射(新 Gallery 运行时走连续 scale)。 */
export const CARD_SIZE_TO_THUMB: Record<CardSize, ThumbSize> = {
  compact: 'sm', balanced: 'md', spacious: 'lg',
}

/** 语义档 → 无级滑杆初值(旧 preset 默认视觉尽量保持:sm/md/lg ≈ 96/140/220px)。 */
export const CARD_SIZE_TO_THUMB_SCALE: Record<CardSize, number> = {
  compact: THUMB_SIZE_TO_SCALE.sm, balanced: THUMB_SIZE_TO_SCALE.md, spacious: THUMB_SIZE_TO_SCALE.lg,
}

// ── 过滤 ───────────────────────────────────────────────────────────────────────

export type ActiveFilter = { field: string; value?: string; values?: string[] }

const TOP_FIELDS = new Set(['id', 'type', 'name', 'version', 'deleted', 'read_only'])

function readPath(source: unknown, path: string): unknown {
  if (!source || typeof source !== 'object') return undefined
  let cur: unknown = source
  for (const part of path.split('.')) {
    if (!cur || typeof cur !== 'object') return undefined
    cur = (cur as Record<string, unknown>)[part]
  }
  return cur
}

/** CardList 字段读取:顶层 entity 字段 + data 字段 + data.xxx 路径;给搜索/筛选/排序共用。 */
export function readCardField(entity: EntityDict, field: string): unknown {
  if (field.startsWith('data.')) return readPath(entity.data, field.slice(5))
  if (TOP_FIELDS.has(field)) return (entity as unknown as Record<string, unknown>)[field]
  if (Object.prototype.hasOwnProperty.call(entity.data, field)) return entity.data[field]
  return readPath(entity.data, field)
}

/** 搜索字段解析:显式 searchFields 优先;缺省 = labelField/descriptionField 去重(name 恒参与)。 */
export function resolveSearchFields(
  searchFields: string[] | undefined, labelField?: string, descriptionField?: string,
): string[] {
  if (searchFields && searchFields.length) return [...new Set(searchFields)]
  return [...new Set([labelField, descriptionField].filter((f): f is string => !!f))]
}

/** query(大小写不敏感子串,覆盖 entity.name + 搜索字段)+ 状态 pill(字段值全等)共同收窄。 */
export function filterCards(
  entities: EntityDict[],
  opts: { query?: string; searchFields?: string[]; active?: ActiveFilter[] },
): EntityDict[] {
  const q = (opts.query ?? '').trim().toLowerCase()
  const fields = opts.searchFields ?? []
  const active = opts.active ?? []
  return entities.filter((e) => {
    for (const { field, value, values } of active) {
      const actual = String(readCardField(e, field) ?? '')
      if (value !== undefined && actual !== value) return false
      if (values && values.length && !values.includes(actual)) return false
    }
    if (!q) return true
    if (e.name.toLowerCase().includes(q)) return true
    return fields.some((f) => {
      const v = readCardField(e, f)
      return v !== undefined && v !== null && String(v).toLowerCase().includes(q)
    })
  })
}

/** 固定过滤条件(view_state.requiredFilters/staticFilters):先于搜索/pill 生效,用于同 entity_type 中分角色列表。 */
export function parseRequiredCardFilters(v: unknown): ActiveFilter[] | undefined {
  if (!Array.isArray(v)) return undefined
  const out: ActiveFilter[] = []
  for (const x of v) {
    if (!x || typeof x !== 'object') continue
    const raw = x as Record<string, unknown>
    if (typeof raw.field !== 'string') continue
    if (typeof raw.value === 'string') {
      out.push({ field: raw.field, value: raw.value })
      continue
    }
    if (Array.isArray(raw.values)) {
      const values = raw.values.filter((item): item is string => typeof item === 'string')
      if (values.length) out.push({ field: raw.field, values })
    }
  }
  return out.length ? out : undefined
}

/** pill 值集:声明 values 优先;缺省按字段对实体去重派生(出现序稳定)。 */
export function filterValues(f: GridFilter, entities: EntityDict[]): string[] {
  if (f.values && f.values.length) return f.values
  const seen: string[] = []
  for (const e of entities) {
    const v = readCardField(e, f.field)
    if (v === undefined || v === null || v === '') continue
    const s = String(v)
    if (!seen.includes(s)) seen.push(s)
  }
  return seen
}

// ── 排序 ───────────────────────────────────────────────────────────────────────

export type SortValueType = 'string' | 'number' | 'natural' | 'enum' | 'boolean'

const NATURAL_COLLATOR = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' })

function sortDirection(sort: CardSort): 1 | -1 {
  return sort.direction === 'desc' ? -1 : 1
}

function nullRank(v: unknown): 0 | 1 {
  return v === undefined || v === null || v === '' ? 1 : 0
}

function compareStrings(a: unknown, b: unknown, natural: boolean): number {
  const aa = String(a)
  const bb = String(b)
  return natural ? NATURAL_COLLATOR.compare(aa, bb) : aa.localeCompare(bb)
}

function compareNumbers(a: unknown, b: unknown): number {
  const aa = typeof a === 'number' ? a : Number(a)
  const bb = typeof b === 'number' ? b : Number(b)
  const aBad = Number.isNaN(aa)
  const bBad = Number.isNaN(bb)
  if (aBad && bBad) return compareStrings(a, b, true)
  if (aBad) return 1
  if (bBad) return -1
  return aa - bb
}

function compareBooleans(a: unknown, b: unknown): number {
  const aa = a === true || a === 'true' || a === 1
  const bb = b === true || b === 'true' || b === 1
  return Number(aa) - Number(bb)
}

function compareEnums(a: unknown, b: unknown, order: readonly string[] | undefined): number {
  if (!order?.length) return compareStrings(a, b, false)
  const aa = String(a)
  const bb = String(b)
  const ai = order.indexOf(aa)
  const bi = order.indexOf(bb)
  const aKnown = ai >= 0
  const bKnown = bi >= 0
  if (aKnown && bKnown) return ai - bi
  if (aKnown) return -1
  if (bKnown) return 1
  return compareStrings(aa, bb, false)
}

export function compareCardSortValues(a: unknown, b: unknown, sort: CardSort): number {
  const ar = nullRank(a)
  const br = nullRank(b)
  if (ar !== br) return ar - br
  if (ar === 1) return 0
  const type: SortValueType = sort.valueType ?? 'natural'
  let cmp = 0
  if (type === 'number') cmp = compareNumbers(a, b)
  else if (type === 'boolean') cmp = compareBooleans(a, b)
  else if (type === 'enum') cmp = compareEnums(a, b, sort.order)
  else cmp = compareStrings(a, b, type === 'natural')
  return cmp * sortDirection(sort)
}

export function resolveDefaultSort(sorts: readonly CardSort[] | undefined, defaultSort: string | undefined): CardSort | undefined {
  if (!sorts?.length) return undefined
  return sorts.find((s) => s.id === defaultSort) ?? sorts[0]
}

/** 稳定排序:同值保持输入顺序,空值永远排最后。 */
export function sortCards(
  entities: EntityDict[],
  sorts: readonly CardSort[] | undefined,
  sortId: string | undefined,
): EntityDict[] {
  const sort = resolveDefaultSort(sorts, sortId)
  if (!sort) return entities
  return entities
    .map((entity, index) => ({ entity, index }))
    .sort((a, b) => {
      const cmp = compareCardSortValues(readCardField(a.entity, sort.field), readCardField(b.entity, sort.field), sort)
      return cmp || a.index - b.index
    })
    .map((row) => row.entity)
}

// ── view_state 解析守卫(slotAdapters 用;非法项静默剔除,mod 配置错不崩 UI) ─────────

const SORT_VALUE_TYPES = ['string', 'number', 'natural', 'enum', 'boolean'] as const

function asSortValueType(v: unknown): SortValueType | undefined {
  return SORT_VALUE_TYPES.includes(v as SortValueType) ? (v as SortValueType) : undefined
}

function asDirection(v: unknown): 'asc' | 'desc' | undefined {
  return v === 'asc' || v === 'desc' ? v : undefined
}

export function parseCardSorts(v: unknown): CardSort[] | undefined {
  if (!Array.isArray(v)) return undefined
  const out: CardSort[] = []
  for (const x of v) {
    if (!x || typeof x !== 'object') continue
    const raw = x as Record<string, unknown>
    if (typeof raw.id !== 'string' || typeof raw.field !== 'string') continue
    if (typeof raw.labelKey !== 'string' && typeof raw.label !== 'string') continue
    const order = Array.isArray(raw.order)
      ? raw.order.filter((item): item is string => typeof item === 'string')
      : undefined
    out.push({
      id: raw.id,
      field: raw.field,
      labelKey: typeof raw.labelKey === 'string' ? raw.labelKey : undefined,
      label: typeof raw.label === 'string' ? raw.label : undefined,
      valueType: asSortValueType(raw.valueType) ?? asSortValueType(raw.type),
      direction: asDirection(raw.direction) ?? asDirection(raw.dir),
      order,
    })
  }
  return out.length ? out : undefined
}

export function parseGridFilters(v: unknown): GridFilter[] | undefined {
  if (!Array.isArray(v)) return undefined
  const out = v.filter((x): x is GridFilter =>
    !!x && typeof x === 'object'
    && typeof (x as GridFilter).id === 'string'
    && typeof (x as GridFilter).labelKey === 'string'
    && typeof (x as GridFilter).field === 'string')
  return out.length ? out : undefined
}

export function parseGalleryActions(v: unknown): GalleryAction[] | undefined {
  if (!Array.isArray(v)) return undefined
  const out = v.filter((x): x is GalleryAction =>
    !!x && typeof x === 'object'
    && typeof (x as GalleryAction).command === 'string'
    && (typeof (x as GalleryAction).labelKey === 'string' || typeof (x as GalleryAction).label === 'string')
    && ((x as GalleryAction).scope === 'selection' || (x as GalleryAction).scope === 'global'))
  return out.length ? out : undefined
}
