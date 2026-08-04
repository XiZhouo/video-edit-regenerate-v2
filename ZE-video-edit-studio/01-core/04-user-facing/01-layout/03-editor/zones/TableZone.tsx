/**
 * TableZone — 表格内容区(functional zone;SPEC §2B `table`)
 *
 * 固定骨架(组件画死):表格结构 + 名称列(Entity.name 框架身份列,见 tableColumns.ts)+ 排序 +
 * 过滤(query)+ 多选高亮 + 运行态骨架/错误占位 +
 * 右键菜单 + 行内动作 + 空态 + 滚动归属。复用现 `.ze-editor__tbl*`(table.css;CSS 暂留 L4)。
 * 拓展点(受约束):`columns: ListColumn[]`(format 闭集)+ `rowActions: RowAction[]`(kind 闭集)
 * + `nameColumn:false` 关名称列。列取值走 readCardField(cardlist 同源:顶层字段先于 data)。
 *
 * 自包含视图:内部按 query 过滤 + 排序,经 onVisibleChange 把可见顺序回吐给编辑器(供其多选快捷键)。
 */

import { Fragment, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { ProgressRing, ErrorPlaceholder } from '../lib/Skeleton'
import { ZeIcon } from '../../lib/zeIcon'
import { ContextMenu, type ContextMenuItem } from '../primitives/ContextMenu'
import { useContextMenu } from '../primitives/useContextMenu'
import { useScrollAffordance } from '../lib/useScrollAffordance'
import { readCardField } from '../lib/cardListModel'
import { entityRunIsActive, isVacantPendingEntity } from '../lib/entityRunPresentation'
import { withNameColumn } from './tableColumns'
import type { DispatchFn, EntityDict, EntityRunStatus } from '../types'
import type { ListColumn, RowAction } from './types'

export type TableZoneProps = {
  entityType: string
  entities: EntityDict[]
  /** 拓展点①:列(format 闭集);无则按 data keys 推导。 */
  columns?: ListColumn[]
  /** 框架身份列(Entity.name)开关;缺省 true 恒定前置(列集已有 name/data.name 列时自动不重复)。 */
  nameColumn?: boolean
  /** 拓展点②:行操作(右键 + 行内);kind 闭集。 */
  rowActions?: RowAction[]
  /** 过滤词(由 toolbar 的 search action 提供)。 */
  query?: string
  selectedSet: Set<string>
  onRowClick: (e: EntityDict, ev: React.MouseEvent) => void
  /** 把当前可见顺序回吐给编辑器(多选快捷键用)。 */
  onVisibleChange?: (ids: string[]) => void
  statusOf?: (id: string) => EntityRunStatus | undefined
  dispatch?: DispatchFn
  /** 空态用的类型显示名。 */
  typeLabel?: string
}

type Sort = { field: string; dir: 'asc' | 'desc' } | null

/** delete/adopt 会破坏或替换正在运行的目标；rename/普通 command 不应被整行连坐。 */
export function rowActionConflictsWithRun(action: RowAction): boolean {
  return action.kind === 'delete' || action.kind === 'adopt'
}

function inferColumns(entities: EntityDict[]): ListColumn[] {
  const fields = new Set<string>()
  for (const e of entities) for (const k of Object.keys(e.data)) fields.add(k)
  return [...fields].map((f) => ({ field: f }))
}
function renderCell(value: unknown, format?: ListColumn['format']): string {
  if (value === null || value === undefined) return ''
  if (format === 'bool' || typeof value === 'boolean') return value ? '✓' : '—'
  if (format === 'json' || (typeof value === 'object' && format !== 'number')) return JSON.stringify(value)
  return String(value)
}
function renderCellNode(value: unknown, format?: ListColumn['format']): ReactNode {
  if (format === 'bool' || typeof value === 'boolean') return value ? <ZeIcon name="check" /> : '—'
  return renderCell(value, format)
}
function cmp(a: unknown, b: unknown): number {
  const na = typeof a === 'number' ? a : Number(a)
  const nb = typeof b === 'number' ? b : Number(b)
  if (Number.isFinite(na) && Number.isFinite(nb)) return na - nb
  return renderCell(a).localeCompare(renderCell(b))
}

export function TableZone({
  entityType, entities, columns, nameColumn = true, rowActions, query = '', selectedSet,
  onRowClick, onVisibleChange, statusOf, dispatch, typeLabel,
}: TableZoneProps): ReactNode {
  const t = useT()
  const cols = withNameColumn(columns ?? inferColumns(entities), nameColumn)
  const colCount = cols.length + 1
  const typeName = typeLabel ?? entityType
  const [sort, setSort] = useState<Sort>(null)
  const tblRef = useScrollAffordance('both')
  const ctxMenu = useContextMenu()
  const [menuId, setMenuId] = useState<string | null>(null)

  // 取值与 cardlist 同源(readCardField):'name' 等顶层字段先于 data 字段;data 字段行为不变。
  const view = useMemo(() => {
    let rows = entities
    const q = query.trim().toLowerCase()
    if (q) rows = rows.filter((e) =>
      e.name.toLowerCase().includes(q) || cols.some((c) => renderCell(readCardField(e, c.field), c.format).toLowerCase().includes(q)))
    if (sort) rows = [...rows].sort((a, b) => cmp(readCardField(a, sort.field), readCardField(b, sort.field)) * (sort.dir === 'asc' ? 1 : -1))
    return rows
  }, [entities, query, sort, cols])

  const viewIds = useMemo(() => view.map((e) => e.id), [view])
  useEffect(() => { onVisibleChange?.(viewIds) }, [viewIds, onVisibleChange])

  const toggleSort = (field: string) =>
    setSort((s) => (s?.field !== field ? { field, dir: 'asc' } : s.dir === 'asc' ? { field, dir: 'desc' } : null))

  // 拓展点执行:把声明式 RowAction 派发为命令(framework 不含业务逻辑)。
  const runAction = (a: RowAction, e: EntityDict) => {
    if (entityRunIsActive(statusOf?.(e.id)) && rowActionConflictsWithRun(a)) return
    if (a.kind === 'delete') { dispatch?.('entity.delete', { type: e.type, id: e.id, expected_version: e.version }); return }
    if (a.kind === 'rename' && a.field) {
      // renameField:'name' = Entity 顶层身份字段(与 readCardField/名称列同一语义),走
      // entity.update 顶层 name 参数(builtin 自带 name 元 patch,undo 完备)——写 data.name
      // 是 schema 外字段,名称列/搜索都读不到,改名等于石沉大海。其余 field 仍是 data 字段。
      const topName = a.field === 'name'
      const cur = topName ? e.name : String(e.data[a.field] ?? e.name ?? '')
      const next = window.prompt(t('editor.rename'), cur)
      if (next != null && next !== cur) {
        const base = { type: e.type, id: e.id, expected_version: e.version }
        dispatch?.('entity.update', topName
          ? { ...base, patches: {}, name: next }
          : { ...base, patches: { [a.field]: next } })
      }
      return
    }
    if (a.command) dispatch?.(a.command, { type: e.type, id: e.id, ...(a.args ?? {}) })
  }

  const del = (e: EntityDict) => {
    if (entityRunIsActive(statusOf?.(e.id))) return
    dispatch?.('entity.delete', { type: e.type, id: e.id, expected_version: e.version })
  }
  const menuItems = (): (ContextMenuItem | 'separator')[] => {
    const target = menuId ? entities.find((x) => x.id === menuId) : null
    if (!target || !rowActions) return []
    const targetRunning = entityRunIsActive(statusOf?.(target.id))
    const menuActs = rowActions.filter((a) => a.placement === 'context-menu')
    const items: (ContextMenuItem | 'separator')[] = []
    for (const a of menuActs.filter((x) => x.kind !== 'delete')) {
      items.push({
        id: a.id, label: t(a.labelKey),
        disabled: targetRunning && rowActionConflictsWithRun(a),
        onSelect: () => runAction(a, target),
      })
    }
    const del = menuActs.find((a) => a.kind === 'delete')
    if (del) {
      if (items.length) items.push('separator')
      items.push({
        id: del.id, label: t(del.labelKey), variant: 'danger', disabled: targetRunning,
        onSelect: () => runAction(del, target),
      })
    }
    return items
  }

  if (entities.length === 0) return <div className="ze-editor__empty">{t('editor.empty', { type: typeName })}</div>
  if (view.length === 0) return <div className="ze-editor__empty">{t('editor.noMatch')}</div>

  return (
    <>
      <div ref={tblRef} className="ze-editor__tbl-scroll ze-scroll-fade-both">
        <table className="ze-editor__tbl ze-table">
          <thead>
            <tr>
              {cols.map((c) => (
                <th key={c.field} data-sortable="true" onClick={() => toggleSort(c.field)}>
                  {c.labelKey ? t(c.labelKey) : c.label ?? c.field}
                  {sort?.field === c.field && <span className="ze-text" data-tone="accent">{' '}<ZeIcon name={sort.dir === 'asc' ? 'sort-asc' : 'sort-desc'} size="xs" /></span>}
                </th>
              ))}
              <th aria-label={t('editor.columnActions')} />
            </tr>
          </thead>
          <tbody>
            {view.map((e) => {
              const st = statusOf?.(e.id)
              const phase = st?.phase
              const runActive = entityRunIsActive(st)
              const vacantPending = isVacantPendingEntity(e, st)
              return (
                <Fragment key={e.id}>
                  <tr
                    className={selectedSet.has(e.id) ? 'is-selected' : undefined}
                    /* W1-2 引导锚点:表格视图下的实体条目,与 ThumbnailGrid 卡片同契约 */
                    data-ze-entity-type={e.type}
                    data-ze-entity-id={e.id}
                    data-multi-selected={selectedSet.size > 1 && selectedSet.has(e.id) ? 'true' : undefined}
                    data-run-phase={phase}
                    aria-busy={runActive ? 'true' : undefined}
                    onClick={(ev) => onRowClick(e, ev)}
                    onContextMenu={rowActions ? (ev) => { setMenuId(e.id); ctxMenu.onContextMenu(ev) } : undefined}
                  >
                    {vacantPending
                      ? cols.map((c) => <td key={c.field}><span className="ze-skeleton" data-variant="text" /></td>)
                      : cols.map((c) => <td key={c.field} data-align={c.align}>{renderCellNode(readCardField(e, c.field), c.format)}</td>)}
                    <td className="ze-editor__row-actions">
                      <span className="ze-editor__row-actions-inner ze-gap" data-gap="1">
                        {runActive && (phase === 'running'
                          ? <ProgressRing progress={st?.progress} size={18} label={st?.stage_label} />
                          : <span className="ze-spinner" data-size="sm" />)}
                        <button type="button" className="ze-icon-button" data-variant="danger" data-size="sm"
                          title={t('editor.delete')} disabled={runActive}
                          onClick={(ev) => { ev.stopPropagation(); del(e) }}><ZeIcon name="close" size="xs" /></button>
                      </span>
                    </td>
                  </tr>
                  {phase === 'error' && (
                    <tr className="ze-editor__tbl-status-row" data-run-phase="error" data-for-entity={e.id}>
                      <td colSpan={colCount}>
                        <ErrorPlaceholder compact error={st?.error}
                          onRetry={st?.run_id ? () => dispatch?.('pipeline.retry_failed', { pipeline_id: st.run_id, item_ids: [e.id] }) : undefined} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>
      {rowActions && <ContextMenu state={ctxMenu.state} items={menuItems()} onClose={ctxMenu.close} menuRef={ctxMenu.menuRef} />}
    </>
  )
}
