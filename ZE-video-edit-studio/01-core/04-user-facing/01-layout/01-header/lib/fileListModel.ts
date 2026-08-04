/**
 * fileListModel — 文件切换器的纯函数模型(header-v2)
 *
 * SPEC: ../SPEC.md §2.3.5(列表排序)+ §2.3.5.7(LRU 规则)+ §2.3.5.8/5.9(行内按钮 gate)
 *
 * 全部纯函数,vitest 直测(ts-unit 门禁);localStorage 读写留在调用方
 * (ProjectContext.openProject 是 LRU 写入唯一漏斗)。
 */

import type { HeaderFileItem } from '../types'

export const RECENT_CAP_MIN = 5
export const RECENT_CAP_MAX = 50

/** 最近列表 cap:钳在 SPEC 范围 5..50(默认 10) */
export function clampRecentCap(cap: number | undefined): number {
  if (!Number.isFinite(cap as number)) return 10
  return Math.min(RECENT_CAP_MAX, Math.max(RECENT_CAP_MIN, Math.trunc(cap as number)))
}

/** LRU 入队:进头、去重移头、超 cap 踢尾。返回新数组(不改原值)。 */
export function pushRecent(ids: readonly string[], id: string, cap?: number): string[] {
  const limit = clampRecentCap(cap)
  return [id, ...ids.filter((x) => x !== id)].slice(0, limit)
}

/**
 * 列表合并排序:LRU 命中段在前(按最近打开序),其余按 updated_at 倒序。
 * 已不可见的 LRU 残留(被删/移出协作)自动过滤;currentId 不参与排序,仅由行高亮用。
 */
export function mergeFileList(
  projects: readonly HeaderFileItem[],
  recentIds: readonly string[],
  cap?: number,
): HeaderFileItem[] {
  const byId = new Map(projects.map((p) => [p.id, p]))
  const limit = clampRecentCap(cap)
  const head: HeaderFileItem[] = []
  const seen = new Set<string>()
  for (const id of recentIds.slice(0, limit)) {
    const item = byId.get(id)
    if (item && !seen.has(id)) {
      head.push(item)
      seen.add(id)
    }
  }
  const rest = projects
    .filter((p) => !seen.has(p.id))
    .slice()
    .sort((a, b) => b.updated_at - a.updated_at)
  return [...head, ...rest]
}

export type RowAction = 'rename' | 'duplicate' | 'save_as' | 'delete'

/** Preserve the row's generation through dialogs and dispatch payloads. */
export function rowActionTarget(item: HeaderFileItem): {
  id: string
  name: string
  generation: number
} {
  return { id: item.id, name: item.name, generation: item.generation }
}

/**
 * 行内按钮 gate:hide 白名单(SPEC §2.6 重载点 4)裁掉 rename/delete;
 * delete 额外要求 is_owner(前端 gate;服务端 owner-only 硬校验是第二道)。
 */
export function rowActions(
  item: HeaderFileItem,
  opts: { hideRename?: boolean; hideDelete?: boolean } = {},
): RowAction[] {
  const out: RowAction[] = []
  if (!opts.hideRename) out.push('rename')
  out.push('duplicate', 'save_as')
  if (!opts.hideDelete && item.is_owner) out.push('delete')
  return out
}
