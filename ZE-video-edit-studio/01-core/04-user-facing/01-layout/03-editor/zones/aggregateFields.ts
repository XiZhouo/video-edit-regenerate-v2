/**
 * aggregateFields — 多选聚合编辑的字段聚合判定(纯逻辑,供 FormZone 调用 + vitest)
 *
 * 同型多 entity 的每个字段:全员同值 → {value, mixed:false}(可直接批量改);
 * 任两员异值 → {value: undefined, mixed:true}(控件显「—」混合占位,首次编辑覆盖全选区)。
 * 值比较用 JSON 序列化(entity.data 是纯 JSON 数据,够用且确定)。
 */

import type { EntityDict, EntityTypeSchema, FieldSchema } from '../types'

export type AggregatedField = { value: unknown; mixed: boolean }

/** 字段全集:schema.properties 有则用之(声明序);否则取所有实体 data keys 并集(出现序)。 */
export function aggregateFieldList(
  entities: EntityDict[],
  schema?: EntityTypeSchema,
): [string, FieldSchema][] {
  if (schema?.properties) return Object.entries(schema.properties)
  const seen: string[] = []
  for (const e of entities) {
    for (const k of Object.keys(e.data ?? {})) {
      if (!seen.includes(k)) seen.push(k)
    }
  }
  return seen.map((k) => [k, {}])
}

export function aggregateFields(
  entities: EntityDict[],
  schema?: EntityTypeSchema,
): Record<string, AggregatedField> {
  const out: Record<string, AggregatedField> = {}
  if (entities.length === 0) return out
  for (const [field] of aggregateFieldList(entities, schema)) {
    const first = JSON.stringify(entities[0].data?.[field] ?? null)
    const same = entities.every((e) => JSON.stringify(e.data?.[field] ?? null) === first)
    out[field] = same
      ? { value: entities[0].data?.[field], mixed: false }
      : { value: undefined, mixed: true }
  }
  return out
}

/** 批量写回 args(entity.batch_update):每实体同一 patches + 各自 expected_version(CAS)。 */
export function batchUpdateArgs(
  entityType: string,
  entities: EntityDict[],
  field: string,
  value: unknown,
): Record<string, unknown> {
  return {
    type: entityType,
    items: entities.map((e) => ({
      id: e.id,
      patches: { [field]: value },
      expected_version: e.version,
    })),
  }
}
