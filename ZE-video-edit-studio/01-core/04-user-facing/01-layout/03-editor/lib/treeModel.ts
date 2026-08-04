/**
 * treeModel — 大纲/层级编辑器(outline)的纯逻辑:扁平实体(parentId/order)↔ 层级树。
 *
 * 抽成纯模块便于单测(vitest node 环境,不拉 React)。outline 编辑器消费这套:
 *   - flattenVisible:据折叠集把树拍平成可见行(带 depth/hasChildren),驱动渲染缩进与 caret。
 *   - isDescendant / computeReparent:拖拽改父的环检测 + CAS 批量 patch(单 Operation 撤销)。
 *
 * 红线:这是「单父层级树」(每节点 ≤1 parent),不是 DAG —— 与 graph 编辑器严格分开
 * (editor-architecture §5;graph 走 DAG/连边,本模型只认 parentId 单链)。
 */

import type { EntityDict } from '../types'

/** view_state 字段映射(闭集):mod 声明实体里哪些字段承载父子/排序/标签/图标。 */
export type TreeFieldMap = {
  /** 父节点 id 字段(值为另一实体 id;空/缺/不在集内 = 根)。默认 'parentId'。 */
  parentField: string
  /** 同层排序字段(number;缺省按 name 兜底)。默认 'order'。 */
  orderField: string
}

export const DEFAULT_TREE_FIELDS: TreeFieldMap = { parentField: 'parentId', orderField: 'order' }

/** 拍平后的一行:渲染所需的最小信息(深度 + 是否有子 + 原实体)。 */
export type TreeRow = {
  entity: EntityDict
  /** 0 = 根层;每下一级 +1(渲染缩进 = depth × 单位)。 */
  depth: number
  /** 有子节点 → 渲 caret(可折叠);无 → 叶子。 */
  hasChildren: boolean
}

function parentIdOf(e: EntityDict, f: TreeFieldMap): string | null {
  const raw = e.data[f.parentField]
  return typeof raw === 'string' && raw ? raw : null
}

function orderOf(e: EntityDict, f: TreeFieldMap): number {
  const raw = e.data[f.orderField]
  return typeof raw === 'number' ? raw : Number.POSITIVE_INFINITY
}

/** 兄弟排序:order 升序,相等/缺失按 name 再按 id 稳定兜底(纯函数无副作用)。 */
function sortSiblings(list: EntityDict[], f: TreeFieldMap): EntityDict[] {
  return [...list].sort((a, b) => {
    const oa = orderOf(a, f), ob = orderOf(b, f)
    if (oa !== ob) return oa - ob
    const na = a.name ?? '', nb = b.name ?? ''
    if (na !== nb) return na < nb ? -1 : 1
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0
  })
}

/** children 索引:parentId('' = 根) → 已排序子实体。根集 key 用空串。 */
export function childrenByParent(
  entities: EntityDict[], fields: Partial<TreeFieldMap> = {},
): Map<string, EntityDict[]> {
  const f = { ...DEFAULT_TREE_FIELDS, ...fields }
  const ids = new Set(entities.map((e) => e.id))
  const buckets = new Map<string, EntityDict[]>()
  for (const e of entities) {
    const pid = parentIdOf(e, f)
    // 父不存在(悬挂引用)→ 当作根,避免节点凭空消失。
    const key = pid && ids.has(pid) ? pid : ''
    const arr = buckets.get(key)
    if (arr) arr.push(e)
    else buckets.set(key, [e])
  }
  for (const [k, arr] of buckets) buckets.set(k, sortSiblings(arr, f))
  return buckets
}

/**
 * 据折叠集把树 DFS 拍平成可见行。collapsed 里的节点其子树不展开(节点本身仍显)。
 * 防御:已访问集杜绝坏数据成环时无限递归(单父模型理论上无环,但悬挂/脏数据要稳)。
 */
export function flattenVisible(
  entities: EntityDict[],
  collapsed: ReadonlySet<string>,
  fields: Partial<TreeFieldMap> = {},
): TreeRow[] {
  const buckets = childrenByParent(entities, fields)
  const rows: TreeRow[] = []
  const seen = new Set<string>()
  const walk = (parentKey: string, depth: number): void => {
    for (const e of buckets.get(parentKey) ?? []) {
      if (seen.has(e.id)) continue
      seen.add(e.id)
      const hasChildren = (buckets.get(e.id)?.length ?? 0) > 0
      rows.push({ entity: e, depth, hasChildren })
      if (hasChildren && !collapsed.has(e.id)) walk(e.id, depth + 1)
    }
  }
  walk('', 0)
  return rows
}

/** nodeId 是否是 candidateAncestorId 的(传递)后代 —— 拖拽改父的环检测基石。 */
export function isDescendant(
  entities: EntityDict[], candidateAncestorId: string, nodeId: string,
  fields: Partial<TreeFieldMap> = {},
): boolean {
  const f = { ...DEFAULT_TREE_FIELDS, ...fields }
  const byId = new Map(entities.map((e) => [e.id, e]))
  let cur = byId.get(nodeId)
  const seen = new Set<string>()
  while (cur) {
    const pid = parentIdOf(cur, f)
    if (!pid || seen.has(pid)) return false
    if (pid === candidateAncestorId) return true
    seen.add(pid)
    cur = byId.get(pid)
  }
  return false
}

/** 一条改动 patch:把 nodeId 重设父+序;带 expected_version 供 CAS。 */
export type ReparentUpdate = {
  id: string
  parentId: string | null
  order: number
  expected_version: number
}

/**
 * 计算把 nodeId 移到 newParentId 下 targetIndex 位的批量 patch(单 Operation 撤销)。
 * 返回 null = 非法(自移/移入自身后代/目标父不存在)或纯 no-op(同父同位)。
 * 策略:把目标新兄弟序列重排成干净 0..n-1,只为 (parentId,order) 真变了的节点出 update。
 */
export function computeReparent(
  entities: EntityDict[],
  nodeId: string,
  newParentId: string | null,
  targetIndex: number,
  fields: Partial<TreeFieldMap> = {},
): ReparentUpdate[] | null {
  const f = { ...DEFAULT_TREE_FIELDS, ...fields }
  const byId = new Map(entities.map((e) => [e.id, e]))
  const node = byId.get(nodeId)
  if (!node) return null
  if (newParentId === nodeId) return null                       // 自己不能做自己的父
  if (newParentId && !byId.has(newParentId)) return null        // 目标父不存在
  if (newParentId && isDescendant(entities, nodeId, newParentId, f)) return null  // 环

  const buckets = childrenByParent(entities, f)
  const destKey = newParentId ?? ''
  // 目标兄弟(去掉被移节点本身),按现序;在 targetIndex 处插入被移节点。
  const dest = (buckets.get(destKey) ?? []).filter((e) => e.id !== nodeId)
  const idx = Math.max(0, Math.min(targetIndex, dest.length))
  dest.splice(idx, 0, node)

  const updates: ReparentUpdate[] = []
  dest.forEach((e, i) => {
    const curParent = parentIdOf(e, f)
    const curOrder = orderOf(e, f)
    if (curParent !== (newParentId ?? null) || curOrder !== i) {
      updates.push({ id: e.id, parentId: newParentId ?? null, order: i, expected_version: e.version })
    }
  })
  return updates.length ? updates : null
}

/** HTML5 拖拽落点区。before/after = 目标的同级置前/后;inside = 成为目标的子(末位)。 */
export type DropZone = 'before' | 'after' | 'inside'

/**
 * 把"拖 nodeId drop 到 targetId 的某区"翻译成 computeReparent 的批量 patch(outline / document 共用)。
 * before/after → 目标同级、按目标在(去掉被拖节点后的)兄弟序里的位置算插入点;inside → 目标的末位子。
 * 目标父悬挂/数据不一致(target 不在该父桶)→ null(不猜位,放弃);自移/移入后代/no-op 由 computeReparent 拦。
 */
export function reparentForDrop(
  entities: EntityDict[], draggedId: string, targetId: string,
  zone: DropZone, fields: Partial<TreeFieldMap> = {},
): ReparentUpdate[] | null {
  const f = { ...DEFAULT_TREE_FIELDS, ...fields }
  const target = entities.find((e) => e.id === targetId)
  if (!target) return null
  const buckets = childrenByParent(entities, f)
  let destParent: string | null
  let targetIndex: number
  if (zone === 'inside') {
    destParent = targetId
    targetIndex = (buckets.get(targetId) ?? []).filter((e) => e.id !== draggedId).length
  } else {
    const tp = target.data[f.parentField]
    destParent = typeof tp === 'string' && tp ? tp : null
    const dest = (buckets.get(destParent ?? '') ?? []).filter((e) => e.id !== draggedId)
    const ti = dest.findIndex((e) => e.id === targetId)
    if (ti < 0) return null
    targetIndex = zone === 'before' ? ti : ti + 1
  }
  return computeReparent(entities, draggedId, destParent, targetIndex, f)
}
