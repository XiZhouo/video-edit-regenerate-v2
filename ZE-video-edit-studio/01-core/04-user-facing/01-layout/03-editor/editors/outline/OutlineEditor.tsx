/**
 * OutlineEditor — 大纲 / 层级树编辑器(outline class;闭 G-C 缺口)
 *
 * 多实体 master 编辑器:把带 parentId/order 的扁平实体集渲成可折叠层级树,选择驱动
 * (与 canvas2d/scene3d 经 selection[nodeType] 联动:outline 管层级+选择,内容编辑各归各)。
 * 严格单父层级(非 DAG;与 graph 分开)。展开折叠是本地视图态,不进 Operation。
 *
 * 受控件:数据/选择 props 进、dispatch 出;不持有 document、不 import 别的编辑器(L3 三律)。
 * 树渲染/缩进/折叠视觉在 OutlinerZone + 03-zones/navigation/outliner;本组件只搭壳 + 接选择/折叠态。
 * reparent(拖拽改父)逻辑在 lib/treeModel.computeReparent(已单测),UI 拖拽待真实消费者再接。
 */

import { useMemo, useRef, useState, type ReactNode } from 'react'
import { EditorFrame } from '../../EditorFrame'
import { HeaderZone } from '../../zones/HeaderZone'
import { ControlZone } from '../../zones/ControlZone'
import { OutlinerZone, type DropZone } from '../../zones/OutlinerZone'
import { useEditorDispatch } from '../../lib/dispatch'
import { useEditorShortcuts, type ShortcutSpec } from '../../lib/useEditorShortcuts'
import { flattenVisible, reparentForDrop } from '../../lib/treeModel'
import type { SelectionMode } from '../../../lib/selectionModel'
import type { ToolbarAction } from '../../zones/types'
import type { AssetUrlFn, DispatchFn, EntityDict, EntityRunStatus } from '../../types'

export type OutlineEditorProps = {
  entityType: string
  /** 编辑器名(layout slot.nameKey 译名;优先于 typeLabel/类型回退,与 gallery/dialog 一致)。 */
  editorName?: string
  typeLabel?: string
  entities: EntityDict[]
  selectedId?: string | null
  multiSelection?: string[]
  onSelect?: (id: string) => void
  onSelectMulti?: (id: string, mode: SelectionMode, orderedIds: string[]) => void
  statusOf?: (id: string) => EntityRunStatus | undefined
  dispatch?: DispatchFn
  /** 父子/排序/图标字段映射(闭集;缺省 parentId/order)。 */
  parentField?: string
  orderField?: string
  labelField?: string
  iconField?: string
  thumbField?: string
  badgeField?: string
  visibilityField?: string
  lockedField?: string
  renameField?: string
  deleteCommand?: string
  duplicateCommand?: string
  assetUrl?: AssetUrlFn
  /** mod 声明的控制命令(ToolbarAction[kind=command]);空则不渲控制区。 */
  actions?: ToolbarAction[]
}

export function OutlineEditor({
  entityType, editorName, typeLabel, entities, selectedId, multiSelection, onSelect, onSelectMulti,
  statusOf, dispatch, parentField, orderField, labelField, iconField, thumbField, badgeField,
  visibilityField, lockedField, renameField, deleteCommand, duplicateCommand, assetUrl, actions,
}: OutlineEditorProps): ReactNode {
  const ctxDispatch = useEditorDispatch()
  const fire = dispatch ?? ctxDispatch
  const typeName = typeLabel ?? entityType
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set())
  const rootRef = useRef<HTMLDivElement>(null)

  const rows = useMemo(
    () => flattenVisible(entities, collapsed, { parentField, orderField }),
    [entities, collapsed, parentField, orderField],
  )
  const selectedSet = useMemo(
    () => new Set(multiSelection && multiSelection.length ? multiSelection : selectedId ? [selectedId] : []),
    [multiSelection, selectedId],
  )
  const viewIds = useMemo(() => rows.map((r) => r.entity.id), [rows])

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

  const toggleCollapse = (id: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const selectNode = (id: string, mode: SelectionMode, orderedIds: string[]) => {
    if (onSelectMulti) onSelectMulti(id, mode, orderedIds)
    else onSelect?.(id)
  }

  const entityById = useMemo(() => new Map(entities.map((e) => [e.id, e])), [entities])
  const patchEntity = (id: string, patches: Record<string, unknown>, name?: string) => {
    const e = entityById.get(id)
    if (!e) return
    fire('entity.update', {
      type: entityType,
      id,
      patches,
      ...(name !== undefined ? { name } : {}),
      expected_version: e.version,
    })
  }
  const toggleBool = (id: string, field: string, defaultVal: boolean) => {
    const e = entityById.get(id)
    if (!e) return
    const cur = typeof e.data[field] === 'boolean' ? (e.data[field] as boolean) : defaultVal
    patchEntity(id, { [field]: !cur })
  }
  const renameNode = renameField ? (id: string, value: string) => {
    const next = value.trim()
    if (!next) return
    if (renameField === 'name' || renameField === '__name') patchEntity(id, {}, next)
    else patchEntity(id, { [renameField]: next })
  } : undefined
  const deleteNode = (id: string) => {
    const e = entityById.get(id)
    if (!e) return
    fire(deleteCommand ?? 'entity.delete', { type: entityType, id, expected_version: e.version })
  }
  const duplicateNode = duplicateCommand ? (id: string) => {
    const e = entityById.get(id)
    if (!e) return
    fire(duplicateCommand, { type: entityType, id, expected_version: e.version })
  } : undefined

  // 拖拽改父:落点区 → reparentForDrop 算 CAS 批量 patch → entity.batch_update(单 Operation 一次 ⌘Z)。
  const pf = parentField ?? 'parentId'
  const of = orderField ?? 'order'
  const reparent = (draggedId: string, targetId: string, zone: DropZone) => {
    const updates = reparentForDrop(entities, draggedId, targetId, zone, { parentField: pf, orderField: of })
    if (!updates) return
    fire('entity.batch_update', {
      type: entityType,
      items: updates.map((u) => ({ id: u.id, patches: { [pf]: u.parentId, [of]: u.order }, expected_version: u.expected_version })),
    })
  }
  const canDrag = (id: string) => {
    if (!lockedField) return true
    return entityById.get(id)?.data[lockedField] !== true
  }

  return (
    <EditorFrame
      editorClass="outline"
      rootRef={rootRef}
      header={<HeaderZone title={editorName ?? typeName} count={entities.length} />}
      zones={actions && actions.length > 0
        ? [<ControlZone key="control" actions={actions} dispatch={fire} />]
        : []}
    >
      <OutlinerZone
        rows={rows}
        selectedSet={selectedSet}
        collapsed={collapsed}
        totalCount={entities.length}
        onSelect={selectNode}
        onToggleCollapse={toggleCollapse}
        statusOf={statusOf}
        labelField={labelField}
        iconField={iconField}
        thumbField={thumbField}
        badgeField={badgeField}
        visibilityField={visibilityField}
        lockedField={lockedField}
        assetUrl={assetUrl}
        onToggleVisible={visibilityField ? (id) => toggleBool(id, visibilityField, true) : undefined}
        onToggleLock={lockedField ? (id) => toggleBool(id, lockedField, false) : undefined}
        onRename={renameNode}
        onDelete={deleteNode}
        onDuplicate={duplicateNode}
        onReparent={reparent}
        canDrag={canDrag}
      />
    </EditorFrame>
  )
}
