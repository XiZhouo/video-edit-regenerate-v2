import type { FieldSchema } from '../types'

type VisibleWhen = {
  field?: unknown
  values?: unknown
  aliases?: Record<string, string>
}

function visibleByField(rule: VisibleWhen, siblingData?: Record<string, unknown>): boolean {
  if (typeof rule.field !== 'string') return true
  if (!Array.isArray(rule.values)) return true
  const raw = String(siblingData?.[rule.field] ?? '')
  const key = rule.aliases?.[raw] ?? raw
  return rule.values.map((v) => String(v)).includes(key)
}

export function fieldVisibleForData(fs: FieldSchema, siblingData?: Record<string, unknown>): boolean {
  const rule = fs['x-ze-visible-when']
  if (!rule || typeof rule !== 'object' || Array.isArray(rule)) return true
  return visibleByField(rule as VisibleWhen, siblingData)
}
