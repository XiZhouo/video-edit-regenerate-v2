/**
 * OutlinerZone — 大纲/层级树内容区(functional zone;SPEC content role `outliner`)
 *
 * 固定骨架:`.ze-outliner` 滚动容器(只管 flex/scroll/role —— 边界由外层 EditorFrame content 持有,
 * 本体不自画 raised 面,避免 content + 内层面双壳)+ 逐节点 `.ze-outliner__node`(深度缩进 +
 * caret 折叠 + 选中 data-active)。复用既有 03-zones/navigation/outliner/code.css 的结构与令牌视觉。
 *
 * 数据驱动:rows 由 treeModel.flattenVisible 拍平(带 depth/hasChildren);缩进 = depth × 单位
 * (行内 paddingInlineStart,calc 令牌×深度);折叠态是**本地视图态**(非 Operation)。选择走统一
 * selectionModel(点击=replace,⌘=toggle,⇧=range),与 table/gallery 同协议;本 zone 只渲不改数据。
 *
 * 拖拽改父(onReparent 注入时启用,HTML5 DnD):拖一节点 drop 到目标行的上/中/下区 → before/inside/after,
 * 由 OutlineEditor 经 treeModel.computeReparent 算 CAS 批量 patch(单 Operation)。落点指示 data-drop(零令牌)。
 */

import { useState, type DragEvent, type KeyboardEvent, type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { ZeIcon } from '../../lib/zeIcon'
import { selectionModeFromEvent } from '../../lib/selectionModel'
import { useScrollAffordance } from '../lib/useScrollAffordance'
import type { TreeRow } from '../lib/treeModel'
import type { SelectionMode } from '../../lib/selectionModel'
import type { AssetRefDict, AssetUrlFn, EntityDict, EntityRunStatus } from '../types'

/** 拖拽落点区:目标行的上区=同级置前、中区=成为其子、下区=同级置后。 */
export type DropZone = 'before' | 'after' | 'inside'

export type OutlinerZoneProps = {
  /** treeModel.flattenVisible 的可见行(顺序即展示顺序,含 depth/hasChildren)。 */
  rows: TreeRow[]
  /** 当前多选集(含单选)。 */
  selectedSet: ReadonlySet<string>
  /** 已折叠节点 id 集(本地视图态)。 */
  collapsed: ReadonlySet<string>
  /** 总节点数:0 → "空";>0 但 rows 空 → 不会发生(根恒可见)。 */
  totalCount: number
  onSelect?: (id: string, mode: SelectionMode, orderedIds: string[]) => void
  onToggleCollapse?: (id: string) => void
  statusOf?: (id: string) => EntityRunStatus | undefined
  labelField?: string
  /** 可选图标字段(entity.data[iconField] = 图库名),无则不渲图标。 */
  iconField?: string
  /** 可选缩略图字段(entity.data[thumbField] = asset_ref)。 */
  thumbField?: string
  /** 可选 badge 字段(entity.data[badgeField] = string/number/bool)。 */
  badgeField?: string
  visibilityField?: string
  lockedField?: string
  assetUrl?: AssetUrlFn
  onToggleVisible?: (id: string) => void
  onToggleLock?: (id: string) => void
  onRename?: (id: string, value: string) => void
  onDelete?: (id: string) => void
  onDuplicate?: (id: string) => void
  /** 拖拽改父(注入时节点可拖):(被拖 id, 目标 id, 落点区)→ OutlineEditor 算 computeReparent。 */
  onReparent?: (draggedId: string, targetId: string, zone: DropZone) => void
  canDrag?: (id: string) => boolean
}

export function OutlinerZone({
  rows, selectedSet, collapsed, totalCount, onSelect, onToggleCollapse, statusOf,
  labelField, iconField, thumbField, badgeField, visibilityField, lockedField, assetUrl,
  onToggleVisible, onToggleLock, onRename, onDelete, onDuplicate, onReparent, canDrag,
}: OutlinerZoneProps): ReactNode {
  const t = useT()
  const orderedIds = rows.map((r) => r.entity.id)
  const [dragId, setDragId] = useState<string | null>(null)
  const [drop, setDrop] = useState<{ id: string; zone: DropZone } | null>(null)
  const [editing, setEditing] = useState<{ id: string; value: string } | null>(null)
  const treeRef = useScrollAffordance('y')

  if (totalCount === 0) {
    return (
      <div ref={treeRef} className="ze-outliner ze-scroll-fade-y ze-stack" role="tree">
        <div className="ze-editor__empty">{t('editor.outlineEmpty')}</div>
      </div>
    )
  }

  const zoneFromEvent = (ev: DragEvent<HTMLDivElement>): DropZone => {
    const r = ev.currentTarget.getBoundingClientRect()
    const y = ev.clientY - r.top
    if (y < r.height * 0.3) return 'before'
    if (y > r.height * 0.7) return 'after'
    return 'inside'
  }
  const clearDrag = () => { setDragId(null); setDrop(null) }
  const labelOf = (entity: EntityDict): string => {
    const value = labelField ? entity.data[labelField] : undefined
    if (value !== undefined && value !== null && value !== '') return String(value)
    return entity.name || entity.id.slice(0, 8)
  }
  const asRef = (value: unknown): AssetRefDict | null =>
    value && typeof value === 'object' && typeof (value as { sha256?: unknown }).sha256 === 'string'
      ? value as AssetRefDict
      : null
  const thumbUrl = (entity: EntityDict): string | undefined => {
    const ref = thumbField ? asRef(entity.data[thumbField]) : null
    return ref && assetUrl ? assetUrl(ref) : undefined
  }
  const badgeOf = (entity: EntityDict): string | null => {
    if (!badgeField) return null
    const value = entity.data[badgeField]
    if (value === undefined || value === null || value === '') return null
    if (typeof value === 'object') return null
    return String(value)
  }
  const commitRename = () => {
    if (!editing) return
    onRename?.(editing.id, editing.value)
    setEditing(null)
  }
  const onRenameKey = (ev: KeyboardEvent<HTMLInputElement>) => {
    if (ev.key === 'Enter') commitRename()
    if (ev.key === 'Escape') { ev.preventDefault(); setEditing(null) }
  }

  return (
    <div ref={treeRef} className="ze-outliner ze-scroll-fade-y ze-stack" role="tree">
      {rows.map(({ entity, depth, hasChildren }) => {
        const expanded = hasChildren && !collapsed.has(entity.id)
        const selected = selectedSet.has(entity.id)
        const icon = iconField ? entity.data[iconField] : undefined
        const st = statusOf?.(entity.id)
        const label = labelOf(entity)
        const thumb = thumbUrl(entity)
        const badge = badgeOf(entity)
        const visible = visibilityField ? entity.data[visibilityField] !== false : true
        const locked = lockedField ? entity.data[lockedField] === true : false
        const dropZone = onReparent && drop?.id === entity.id && dragId && dragId !== entity.id ? drop.zone : undefined
        const draggable = !!(onReparent && (canDrag ? canDrag(entity.id) : true))
        return (
          <div
            key={entity.id}
            className="ze-menu-item ze-outliner__node"
            role="treeitem"
            aria-level={depth + 1}
            aria-expanded={hasChildren ? expanded : undefined}
            aria-selected={selected}
            data-active={selected ? 'true' : undefined}
            data-expanded={expanded ? 'true' : undefined}
            data-hidden={!visible ? 'true' : undefined}
            data-locked={locked ? 'true' : undefined}
            data-drop={dropZone}
            draggable={draggable ? true : undefined}
            style={{ paddingInlineStart: `calc(var(--ze-space-2) + ${depth} * var(--ze-space-4))` }}
            onClick={(ev) => { if (ev.detail > 1) return; onSelect?.(entity.id, selectionModeFromEvent(ev), orderedIds) }} // 双击第二击不改选择(重命名不被再击取消打断)
            onDragStart={draggable ? (ev) => { setDragId(entity.id); ev.dataTransfer.effectAllowed = 'move'; ev.dataTransfer.setData('text/plain', entity.id) } : undefined}
            onDragOver={onReparent ? (ev) => { if (dragId && dragId !== entity.id) { ev.preventDefault(); ev.stopPropagation(); ev.dataTransfer.dropEffect = 'move'; setDrop({ id: entity.id, zone: zoneFromEvent(ev) }) } } : undefined}
            onDrop={onReparent ? (ev) => { ev.preventDefault(); ev.stopPropagation(); if (dragId && dragId !== entity.id) onReparent(dragId, entity.id, zoneFromEvent(ev)); clearDrag() } : undefined}
            onDragEnd={draggable ? clearDrag : undefined}
          >
            {hasChildren ? (
              <button
                type="button"
                className="ze-outliner__caret"
                aria-label={t(expanded ? 'editor.outlineCollapse' : 'editor.outlineExpand')}
                onClick={(ev) => { ev.stopPropagation(); onToggleCollapse?.(entity.id) }}
              >
                <ZeIcon name="chevron-right" size="xs" />
              </button>
            ) : (
              <span className="ze-outliner__caret" aria-hidden="true" />
            )}
            {onToggleVisible ? (
              <button
                type="button"
                className="ze-outliner__action"
                aria-label={t(visible ? 'editor.hide' : 'editor.show')}
                title={t(visible ? 'editor.hide' : 'editor.show')}
                onClick={(ev) => { ev.stopPropagation(); onToggleVisible(entity.id) }}
              >
                <ZeIcon name={visible ? 'eye' : 'eye-off'} size="xs" />
              </button>
            ) : null}
            {onToggleLock ? (
              <button
                type="button"
                className="ze-outliner__action"
                aria-label={t(locked ? 'editor.unlock' : 'editor.lock')}
                title={t(locked ? 'editor.unlock' : 'editor.lock')}
                onClick={(ev) => { ev.stopPropagation(); onToggleLock(entity.id) }}
              >
                <ZeIcon name={locked ? 'lock' : 'unlock'} size="xs" />
              </button>
            ) : null}
            {thumb ? <img className="ze-outliner__thumb" src={thumb} alt="" draggable={false} /> : null}
            {typeof icon === 'string' && icon ? <ZeIcon name={icon} size="sm" /> : null}
            {editing?.id === entity.id ? (
              <input
                className="ze-outliner__rename ze-input"
                value={editing.value}
                autoFocus
                onClick={(ev) => ev.stopPropagation()}
                onChange={(ev) => setEditing({ id: entity.id, value: ev.target.value })}
                onBlur={commitRename}
                onKeyDown={onRenameKey}
              />
            ) : (
              <span
                className="ze-outliner__label ze-text"
                data-role="secondary"
                onDoubleClick={onRename ? (ev) => { ev.stopPropagation(); setEditing({ id: entity.id, value: label }) } : undefined}
              >
                {label}
              </span>
            )}
            {badge ? <span className="ze-outliner__badge ze-text" data-size="xs" data-role="secondary">{badge}</span> : null}
            {st?.phase === 'running'
              ? <span className="ze-icon" data-icon="dot" data-size="xs" data-tone="accent" aria-hidden="true" />
              : null}
            {onDuplicate ? (
              <button
                type="button"
                className="ze-outliner__action"
                aria-label={t('editor.duplicate')}
                title={t('editor.duplicate')}
                onClick={(ev) => { ev.stopPropagation(); onDuplicate(entity.id) }}
              >
                <ZeIcon name="copy" size="xs" />
              </button>
            ) : null}
            {onDelete ? (
              <button
                type="button"
                className="ze-outliner__action"
                aria-label={t('editor.delete')}
                title={t('editor.delete')}
                onClick={(ev) => { ev.stopPropagation(); onDelete(entity.id) }}
              >
                <ZeIcon name="trash" size="xs" />
              </button>
            ) : null}
          </div>
        )
      })}
    </div>
  )
}
