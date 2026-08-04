/**
 * GraphEditor — `graph` editor class
 *
 * 关系型(节点 + 边):节点是可独立选中的 entity,按 view_state 字段映射 x/y 自由定位;
 * 边由每个节点的 deps 字段(目标节点 id 数组)渲染成 SVG 连线。本质是 2D 自由定位版的
 * 空间编辑器。不引第三方图库(core 不打包重库;力导向布局/复杂连线属重库,留 mod/lazyModule)。
 *
 * zone 化(P3⑤):外壳=`EditorFrame`(承接局部快捷键的 rootRef);身份行=`HeaderZone`(title+count);
 * 画布与节点拖动=`GraphCanvasZone`(自持 drag draft + pointerup CAS 提交)。删除快捷键留本编辑器
 * (它持 rootRef + selectedId/deleteCommand,见 useEditorShortcuts 作用域)。
 */

import { useMemo, useRef } from 'react'
import { useT } from '@ze/core/04-user-facing/05-preferences'
import { useEditorDispatch } from '../../lib/dispatch'
import { useEditorShortcuts } from '../../lib/useEditorShortcuts'
import { EditorFrame } from '../../EditorFrame'
import { HeaderZone } from '../../zones/HeaderZone'
import { GraphCanvasZone, type GraphFields } from '../../zones/GraphCanvasZone'
import type { DispatchFn, EntityDict, EntityRunStatus } from '../../types'

export type { GraphFields }

export type GraphEditorProps = {
  entityType: string
  /** 编辑器名(layout slot.nameKey 译名;优先显示,与其它编辑器 header 一致)。 */
  editorName?: string
  typeLabel?: string
  entities: EntityDict[]
  selectedId: string | null
  onSelect?: (id: string | null) => void
  fields: GraphFields
  addCommand?: string
  updateCommand?: string
  deleteCommand?: string
  dispatch?: DispatchFn
  statusOf?: (id: string) => EntityRunStatus | undefined
}

export function GraphEditor({
  entityType, editorName, typeLabel, entities, selectedId, onSelect, fields,
  addCommand, updateCommand, deleteCommand, dispatch, statusOf,
}: GraphEditorProps) {
  const t = useT()
  const ctxDispatch = useEditorDispatch()
  const fire = dispatch ?? ctxDispatch
  const rootRef = useRef<HTMLDivElement>(null)

  // 删除选中节点(Del/Backspace,带 expected_version CAS);编辑器持 rootRef 故快捷键留此层。
  const shortcuts = useMemo(() => {
    const del = () => {
      if (!selectedId || !deleteCommand) return
      const e = entities.find((x) => x.id === selectedId)
      if (e) fire(deleteCommand, { type: entityType, id: e.id, expected_version: e.version })
    }
    return [{ key: 'Delete', handler: del }, { key: 'Backspace', handler: del }]
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, entities, deleteCommand, entityType, fire])
  useEditorShortcuts(rootRef, shortcuts)

  if (entities.length === 0) {
    return (
      <EditorFrame editorClass="graph" rootRef={rootRef} header={<HeaderZone title={editorName ?? typeLabel ?? entityType} />}>
        <EditorFrame.Empty>{t('editor.noGraph')}</EditorFrame.Empty>
      </EditorFrame>
    )
  }

  return (
    <EditorFrame editorClass="graph" rootRef={rootRef}
      header={<HeaderZone title={editorName ?? typeLabel ?? entityType} count={entities.length} />}>
      <GraphCanvasZone
        entityType={entityType} entities={entities} selectedId={selectedId} onSelect={onSelect}
        fields={fields} addCommand={addCommand} updateCommand={updateCommand} dispatch={fire} statusOf={statusOf}
      />
    </EditorFrame>
  )
}
