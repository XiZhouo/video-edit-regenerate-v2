/**
 * entityRunPresentation — entity 运行态的内容保留判定。
 *
 * Pipeline pending 既可能是刚创建的空占位，也可能是已有实体排队重跑。两者不能只按
 * phase 区分：前者才需要骨架，后者必须继续显示真实内容。显式 placeholder 身份由
 * Supervisor 在实际新建实体时签发并随 manifest 恢复；前端绝不从文案、名字或 id 猜测。
 */

import type { EntityDict, EntityRunStatus } from '../types'

function hasSubstantiveValue(value: unknown, seen: Set<object>): boolean {
  if (value === null || value === undefined) return false
  if (typeof value === 'string') return value.trim().length > 0
  // false 与 0 都可能是完整业务值，不能用 truthy 判定误删。
  if (typeof value === 'boolean') return true
  if (typeof value === 'number') return Number.isFinite(value)
  if (typeof value !== 'object') return true
  if (seen.has(value)) return false
  seen.add(value)
  if (Array.isArray(value)) return value.some((item) => hasSubstantiveValue(item, seen))
  return Object.values(value as Record<string, unknown>)
    .some((item) => hasSubstantiveValue(item, seen))
}

/** entity 是否已有应保留、可供查看/编辑的真实显示内容。 */
export function entityHasRenderableContent(entity: EntityDict): boolean {
  if (entity.name.trim().length > 0) return true
  return hasSubstantiveValue(entity.data, new Set())
}

/**
 * 只有「pending + runtime 明确签发 placeholder」才用骨架。空 name/data 不是身份：
 * marker 缺失或为 false 时一律保留实体，避免把合法的空白业务对象猜成占位。
 */
export function isVacantPendingEntity(_entity: EntityDict, status?: EntityRunStatus): boolean {
  return status?.phase === 'pending' && status.placeholder === true
}

/** 会与删除/替换目标冲突的活动运行相位。 */
export function entityRunIsActive(status?: EntityRunStatus): boolean {
  return status?.phase === 'pending' || status?.phase === 'running'
}
