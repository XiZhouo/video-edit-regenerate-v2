/**
 * DocumentEditor — 块文档编辑器(document class;Notion/Obsidian/飞书 式 块=L2 atom=实体)
 *
 * 多实体 master:把 block 实体(parent/order 单父树)渲成文档流,每块经块注册表 → L2 原子
 * (.ze-text[data-prose] 接管 markdown 渲染)。Notion 式逐块内联编辑:聚焦块=源 markdown 文本域、
 * 失焦=渲染视图(规避 contentEditable 的光标/IME 坑)。选择驱动(selection[block]),写走 dispatch CAS。
 *
 * 受控件:数据/选择 props 进、dispatch 出;不持有 document、不 import 别的编辑器(L3 三律)。
 * 块视觉只住 L2(渲染器 lib/markdown + 02-atoms text[data-prose]);本组件零样式,只搭壳 + 编辑态。
 * M0:渲块流 + 选择 + 逐块内联编辑(draft/commit CAS)+ 增/删。M1 补 拖拽重排/缩进/4 块种/slash 菜单。
 */

import { useMemo, useState, type DragEvent, type ReactNode } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { EditorFrame } from '../../EditorFrame'
import { HeaderZone } from '../../zones/HeaderZone'
import { useEditorDispatch } from '../../lib/dispatch'
import { flattenVisible, reparentForDrop, type DropZone } from '../../lib/treeModel'
import { renderBlockHtml, blockTypeOf, blockIsTextEditable, type BlockFieldMap } from './blockRegistry'
import { useScrollAffordance } from '../../lib/useScrollAffordance'
import { selectionModeFromEvent, type SelectionMode } from '../../../lib/selectionModel'
import type { DispatchFn, EntityDict, EntityRunStatus } from '../../types'

const EMPTY_SET: ReadonlySet<string> = new Set()

export type DocumentEditorProps = {
  entityType: string
  /** 编辑器名(layout slot.nameKey 译名;优先显示,与其它编辑器 header 一致)。 */
  editorName?: string
  typeLabel?: string
  entities: EntityDict[]
  selectedId?: string | null
  multiSelection?: string[]
  onSelect?: (id: string) => void
  onSelectMulti?: (id: string, mode: SelectionMode, orderedIds: string[]) => void
  /** 新增块的框架接缝(slotAdapters 接 ctx.onAddFor;无则不渲新增条)。 */
  onAdd?: () => void
  statusOf?: (id: string) => EntityRunStatus | undefined
  dispatch?: DispatchFn
  /** 字段映射(闭集;缺省 block archetype binding)。 */
  blockTypeField?: string
  textField?: string
  parentField?: string
  orderField?: string
  mediaField?: string
}

export function DocumentEditor({
  entityType, editorName, typeLabel, entities, selectedId, multiSelection, onSelect, onSelectMulti,
  onAdd, statusOf, dispatch, blockTypeField = 'blockType', textField = 'text',
  parentField = 'parent', orderField = 'order', mediaField = 'media',
}: DocumentEditorProps): ReactNode {
  const t = useT()
  const ctxDispatch = useEditorDispatch()
  const fire = dispatch ?? ctxDispatch
  const typeName = typeLabel ?? entityType
  const fields: BlockFieldMap = { blockTypeField, textField, mediaField }
  const bodyRef = useScrollAffordance('y')

  const [editingId, setEditingId] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  // 进编辑时快照 {版本, 原文}:提交按开编辑那一刻的版本做 CAS —— 编辑期间该块被 SSE 外部改版,
  // expected_version 不匹配 → CAS 如实拒、SSE 重喂(而非用新版本静默盖掉他人改动);文本比对同理用快照原文。
  const [editStart, setEditStart] = useState<{ version: number; text: string }>({ version: 0, text: '' })

  // M0 不折叠(无 toggle 块);仍走 flattenVisible 取文档顺序 + depth 缩进。
  const rows = useMemo(
    () => flattenVisible(entities, EMPTY_SET, { parentField, orderField }),
    [entities, parentField, orderField],
  )
  const selectedSet = useMemo(
    () => new Set(multiSelection && multiSelection.length ? multiSelection : selectedId ? [selectedId] : []),
    [multiSelection, selectedId],
  )
  const orderedIds = useMemo(() => rows.map((r) => r.entity.id), [rows])

  const rawText = (e: EntityDict) => (typeof e.data[textField] === 'string' ? (e.data[textField] as string) : '')

  const beginEdit = (e: EntityDict) => {
    setEditingId(e.id)
    setDraft(rawText(e))
    setEditStart({ version: e.version, text: rawText(e) })
  }
  const commitEdit = (e: EntityDict) => {
    setEditingId(null)
    if (draft === editStart.text) return
    fire('entity.update', { type: e.type, id: e.id, patches: { [textField]: draft }, expected_version: editStart.version })
  }
  const selectBlock = (id: string, mode: SelectionMode) => {
    if (onSelectMulti) onSelectMulti(id, mode, orderedIds)
    else onSelect?.(id)
  }
  const deleteBlock = (e: EntityDict) =>
    fire('entity.delete', { type: e.type, id: e.id, expected_version: e.version })

  // 块拖拽重排/缩进:落点区 → reparentForDrop → entity.batch_update(单 Operation/逐块 CAS,与 outline 共用)。
  const [dragId, setDragId] = useState<string | null>(null)
  const [drop, setDrop] = useState<{ id: string; zone: DropZone } | null>(null)
  const zoneFromEvent = (ev: DragEvent<HTMLDivElement>): DropZone => {
    const r = ev.currentTarget.getBoundingClientRect()
    const y = ev.clientY - r.top
    return y < r.height * 0.3 ? 'before' : y > r.height * 0.7 ? 'after' : 'inside'
  }
  const clearDrag = () => { setDragId(null); setDrop(null) }
  const reparentBlock = (draggedId: string, targetId: string, zone: DropZone) => {
    const updates = reparentForDrop(entities, draggedId, targetId, zone, { parentField, orderField })
    if (!updates) return
    fire('entity.batch_update', {
      type: entityType,
      items: updates.map((u) => ({ id: u.id, patches: { [parentField]: u.parentId, [orderField]: u.order }, expected_version: u.expected_version })),
    })
  }

  return (
    <EditorFrame
      editorClass="document"
      header={<HeaderZone title={editorName ?? typeName} count={entities.length} />}
    >
      {/* 内容流(非纸张):块直接落在 content 的 sunken 面,EditorFrame content 持唯一边界,body 只管 flex/scroll。 */}
      <div ref={bodyRef} className="ze-document__body ze-scroll-fade-y ze-stack">
        {rows.length === 0 ? (
          <div className="ze-editor__empty">{t('editor.noContent')}</div>
        ) : (
          rows.map(({ entity, depth }) => {
            const selected = selectedSet.has(entity.id)
            const editing = editingId === entity.id
            const editable = blockIsTextEditable(blockTypeOf(entity, fields))
            const st = statusOf?.(entity.id)
            const dropZone = drop?.id === entity.id && dragId && dragId !== entity.id ? drop.zone : undefined
            return (
              <div
                key={entity.id}
                className="ze-document__block"
                data-active={selected ? 'true' : undefined}
                data-run-phase={st?.phase}
                data-drop={dropZone}
                draggable={!editing ? true : undefined}
                style={{ paddingInlineStart: `calc(${depth} * var(--ze-space-5))` }}
                onDragStart={!editing ? (ev) => { setDragId(entity.id); ev.dataTransfer.effectAllowed = 'move'; ev.dataTransfer.setData('text/plain', entity.id) } : undefined}
                onDragOver={(ev) => { if (dragId && dragId !== entity.id) { ev.preventDefault(); ev.stopPropagation(); ev.dataTransfer.dropEffect = 'move'; setDrop({ id: entity.id, zone: zoneFromEvent(ev) }) } }}
                onDrop={(ev) => { ev.preventDefault(); ev.stopPropagation(); if (dragId && dragId !== entity.id) reparentBlock(dragId, entity.id, zoneFromEvent(ev)); clearDrag() }}
                onDragEnd={clearDrag}
              >
                {editing ? (
                  <textarea
                    className="ze-textarea ze-document__edit"
                    autoFocus
                    value={draft}
                    disabled={entity.read_only}
                    onChange={(ev) => setDraft(ev.target.value)}
                    onBlur={() => commitEdit(entity)}
                  />
                ) : (
                  <div
                    className="ze-document__view ze-text"
                    data-prose="true"
                    role="button"
                    tabIndex={0}
                    onClick={(ev) => { selectBlock(entity.id, selectionModeFromEvent(ev)); if (editable && !entity.read_only) beginEdit(entity) }}
                    dangerouslySetInnerHTML={{ __html: renderBlockHtml(entity, fields) }}
                  />
                )}
                {selected && !entity.read_only ? (
                  <button
                    type="button"
                    className="ze-button ze-document__del"
                    data-variant="ghost"
                    data-size="sm"
                    aria-label={t('editor.delete')}
                    onClick={() => deleteBlock(entity)}
                  >
                    ×
                  </button>
                ) : null}
              </div>
            )
          })
        )}
        {onAdd ? (
          <button type="button" className="ze-button ze-document__add" data-variant="ghost" onClick={onAdd}>
            + {t('editor.blockAdd')}
          </button>
        ) : null}
      </div>
    </EditorFrame>
  )
}
