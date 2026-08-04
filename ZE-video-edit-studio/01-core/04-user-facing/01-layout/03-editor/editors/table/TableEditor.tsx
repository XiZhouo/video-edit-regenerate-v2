/**
 * TableEditor — `table` editor class(声明式组合功能区)
 *
 * 列表型:某 entity_type 的多 entity 多列展示。本编辑器 = EditorFrame 外壳 + HeaderZone(身份)
 * + ToolRowZone(搜索/新增操作区)
 * + TableZone(表格内容区)的声明式组合(SPEC §2.5)。
 * 编辑器只负责:选择模型 + 快捷键 + 声明 toolbar/row 的拓展点(actions / rowActions);
 * 排序/过滤/运行态/右键渲染等固定骨架在 TableZone 内。视图态(query)不 dispatch;
 * 领域写(删/复制/改名)走 command dispatch。
 */

import { useMemo, useRef, useState } from 'react'
import { useEditorDispatch } from '../../lib/dispatch'
import { useEditorShortcuts, type ShortcutSpec } from '../../lib/useEditorShortcuts'
import { selectionModeFromEvent, type SelectionMode } from '../../../lib/selectionModel'
import { EditorFrame } from '../../EditorFrame'
import { HeaderZone } from '../../zones/HeaderZone'
import { ToolRowZone } from '../../zones/ToolRowZone'
import { TableZone } from '../../zones/TableZone'
import type { ToolbarAction, RowAction } from '../../zones/types'
import type { Column, DispatchFn, EntityDict, EntityRunStatus } from '../../types'

export type TableEditorProps = {
  entityType: string
  /** 编辑器名(layout slot.nameKey 译名;优先显示,与其它编辑器 header 一致)。 */
  editorName?: string
  /** 类型显示名(数据显示名,mod 经 schema.label 提供;无则回退裸 entityType)。 */
  typeLabel?: string
  entities: EntityDict[]
  columns?: Column[]
  /** 框架身份列(Entity.name)开关;缺省 true(TableZone 恒定前置,已有 name 列自动不重复)。 */
  nameColumn?: boolean
  selectedId?: string | null
  onSelect?: (id: string) => void
  /** 多选(P0):传了优先于单选;(id, mode, 当前可见顺序)*/
  multiSelection?: string[]
  onSelectMulti?: (id: string, mode: SelectionMode, orderedIds: string[]) => void
  onAdd?: () => void
  dispatch?: DispatchFn
  statusOf?: (id: string) => EntityRunStatus | undefined
  /** 右键菜单:复制命令 id(mod 声明);改名字段(mod 声明,data 内的名字字段)*/
  duplicateCommand?: string
  renameField?: string
}

export function TableEditor({
  entityType, editorName, typeLabel, entities, columns, nameColumn, selectedId, onSelect, multiSelection, onSelectMulti,
  onAdd, dispatch, statusOf, duplicateCommand, renameField,
}: TableEditorProps) {
  const ctxDispatch = useEditorDispatch()
  const fire = dispatch ?? ctxDispatch
  const typeName = typeLabel ?? entityType
  const [query, setQuery] = useState('')
  const [viewIds, setViewIds] = useState<string[]>([])

  const selectedSet = useMemo(
    () => new Set(multiSelection && multiSelection.length ? multiSelection : selectedId ? [selectedId] : []),
    [multiSelection, selectedId],
  )

  // 完整多选:⌘A 全选可见 / ⌘⇧A 反选 / Esc 清空(可见顺序由 TableZone 经 onVisibleChange 回吐)。
  const rootRef = useRef<HTMLDivElement>(null)
  const shortcuts = useMemo<ShortcutSpec[]>(() => {
    if (!onSelectMulti) return []
    const all = (e: KeyboardEvent) => { e.preventDefault(); onSelectMulti('', 'all', viewIds) }
    const inv = (e: KeyboardEvent) => { e.preventDefault(); onSelectMulti('', 'invert', viewIds) }
    return [
      { key: 'a', meta: true, handler: all },
      { key: 'a', ctrl: true, handler: all },
      { key: 'a', meta: true, shift: true, handler: inv },
      { key: 'a', ctrl: true, shift: true, handler: inv },
      { key: 'Escape', handler: () => onSelectMulti('', 'none', viewIds) },
    ]
  }, [onSelectMulti, viewIds])
  useEditorShortcuts(rootRef, shortcuts)

  const rowClick = (e: EntityDict, ev: React.MouseEvent) => {
    if (onSelectMulti) onSelectMulti(e.id, selectionModeFromEvent(ev), viewIds)
    else onSelect?.(e.id)
  }

  // 拓展点:行操作(右键菜单);mod 经 duplicateCommand/renameField 声明,删除恒在。
  const rowActions = useMemo<RowAction[]>(() => {
    const acts: RowAction[] = []
    if (duplicateCommand) acts.push({ id: 'dup', labelKey: 'editor.duplicate', kind: 'command', command: duplicateCommand, placement: 'context-menu' })
    if (renameField) acts.push({ id: 'rename', labelKey: 'editor.rename', kind: 'rename', field: renameField, placement: 'context-menu' })
    acts.push({ id: 'del', labelKey: 'editor.delete', kind: 'delete', variant: 'danger', placement: 'context-menu' })
    return acts
  }, [duplicateCommand, renameField])

  // 拓展点:操作条(搜索 + 新增)。onAdd 是编辑器内建回调,经本地 dispatch 包装路由(ToolbarZone 契约只见声明式 action)。
  const toolbarDispatch: DispatchFn = (cmd, args) => {
    if (cmd === '__add' && onAdd) { onAdd(); return }
    fire(cmd, args)
  }
  const toolbarActions = useMemo<ToolbarAction[]>(() => {
    const acts: ToolbarAction[] = [{ id: 'search', kind: 'search', viewKey: 'query', labelKey: 'editor.search' }]
    if (onAdd) acts.push({ id: 'add', kind: 'command', command: '__add', labelKey: 'editor.add', icon: '+' })
    return acts
  }, [onAdd])

  const count = selectedSet.size > 1 ? `${selectedSet.size}/${entities.length}` : entities.length

  return (
    <EditorFrame
      editorClass="table" rootRef={rootRef}
      header={<HeaderZone title={editorName ?? typeName} count={count} />}
      zones={[
        <ToolRowZone
          key="tools"
          commands={toolbarActions} dispatch={toolbarDispatch}
          viewState={{ query }} onViewChange={(k, v) => { if (k === 'query') setQuery(String(v)) }}
        />,
      ]}
    >
      <TableZone
        entityType={entityType} typeLabel={typeName} entities={entities} columns={columns} nameColumn={nameColumn}
        rowActions={rowActions} query={query} selectedSet={selectedSet} onRowClick={rowClick}
        onVisibleChange={setViewIds} statusOf={statusOf} dispatch={fire}
      />
    </EditorFrame>
  )
}
